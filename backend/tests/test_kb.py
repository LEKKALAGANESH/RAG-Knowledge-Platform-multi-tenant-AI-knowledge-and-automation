import uuid

import httpx

from app.core.db import SessionLocal, use_tenant
from app.kb.models import Document
from tests.conftest import Account, register


async def _seed_doc(acct: Account, kb_id: str) -> uuid.UUID:
    async with SessionLocal() as s:
        await use_tenant(s, acct.org_id, acct.user_id)
        doc = Document(
            org_id=acct.org_id,
            kb_id=uuid.UUID(kb_id),
            name="a.pdf",
            mime="application/pdf",
            sha256="0" * 64,
            blob_key="k",
            warnings=[],
            meta={},
        )
        s.add(doc)
        await s.commit()
        return doc.id


async def test_kb_crud(client: httpx.AsyncClient) -> None:
    a = await register(client)
    r = await client.post(
        "/v1/kbs", json={"name": "Handbook", "settings": {"lang": "en"}}, headers=a.headers
    )
    assert r.status_code == 201
    kb = r.json()
    assert kb["settings"] == {"lang": "en"} and kb["embedding_index_id"] is None

    assert (await client.get(f"/v1/kbs/{kb['id']}", headers=a.headers)).json()["name"] == "Handbook"
    p = await client.patch(
        f"/v1/kbs/{kb['id']}", json={"name": "Policies", "settings": {}}, headers=a.headers
    )
    assert p.json()["name"] == "Policies" and p.json()["settings"] == {}

    listed = (await client.get("/v1/kbs", headers=a.headers)).json()
    assert [k["id"] for k in listed["items"]] == [kb["id"]] and listed["next_cursor"] is None

    assert (await client.delete(f"/v1/kbs/{kb['id']}", headers=a.headers)).status_code == 204
    assert (await client.get(f"/v1/kbs/{kb['id']}", headers=a.headers)).status_code == 404


async def test_kb_pagination(client: httpx.AsyncClient) -> None:
    a = await register(client)
    ids = [
        (await client.post("/v1/kbs", json={"name": f"kb{i}"}, headers=a.headers)).json()["id"]
        for i in range(5)
    ]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/v1/kbs", params=params, headers=a.headers)).json()
        seen += [k["id"] for k in page["items"]]
        if not (cursor := page["next_cursor"]):
            break
    assert seen == list(reversed(ids))
    bad = await client.get("/v1/kbs", params={"cursor": "not-a-cursor"}, headers=a.headers)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_cursor"


async def test_documents_list_get_delete(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "Docs"}, headers=a.headers)).json()
    doc_id = await _seed_doc(a, kb["id"])

    docs = (await client.get(f"/v1/kbs/{kb['id']}/documents", headers=a.headers)).json()
    assert [d["id"] for d in docs["items"]] == [str(doc_id)]
    got = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert got["status"] == "queued" and got["name"] == "a.pdf"

    assert (await client.delete(f"/v1/documents/{doc_id}", headers=a.headers)).status_code == 204
    assert (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).status_code == 404


async def test_cross_org_ids_are_404(client: httpx.AsyncClient) -> None:
    """AC1 at the API level: org B gets 404 (not 403) for every org A resource id."""
    a, b = await register(client), await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "Secret"}, headers=a.headers)).json()
    doc_id = await _seed_doc(a, kb["id"])

    for method, url in [
        ("GET", f"/v1/kbs/{kb['id']}"),
        ("PATCH", f"/v1/kbs/{kb['id']}"),
        ("DELETE", f"/v1/kbs/{kb['id']}"),
        ("GET", f"/v1/kbs/{kb['id']}/documents"),
        ("GET", f"/v1/documents/{doc_id}"),
        ("DELETE", f"/v1/documents/{doc_id}"),
    ]:
        r = await client.request(
            method, url, headers=b.headers, json={} if method == "PATCH" else None
        )
        assert r.status_code == 404, (method, url, r.status_code)
    assert (await client.get("/v1/kbs", headers=b.headers)).json()["items"] == []
    # org A's data is untouched by B's attempts
    assert (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).status_code == 200
