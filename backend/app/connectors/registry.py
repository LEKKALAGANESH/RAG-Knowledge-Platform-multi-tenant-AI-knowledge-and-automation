from app.connectors.base import Connector
from app.connectors.dropbox import DropboxConnector
from app.connectors.gdrive import GDriveConnector
from app.connectors.onedrive import OneDriveConnector
from app.connectors.postgres import PostgresConnector
from app.connectors.rest import RestConnector
from app.connectors.s3 import S3Connector
from app.connectors.website import WebsiteConnector
from app.core.config import get_settings

# Only connectors that really work are listed; credential-gated ones join behind a feature flag.
REGISTRY: dict[str, Connector] = {
    c.meta.type: c for c in (WebsiteConnector(), S3Connector(), PostgresConnector())
}
if get_settings().connector_gdrive_enabled:  # needs the owner's Google OAuth client (A7a)
    REGISTRY["gdrive"] = GDriveConnector()
if get_settings().connector_onedrive_enabled:  # needs the owner's Entra app registration (A7b)
    REGISTRY["onedrive"] = OneDriveConnector()
if get_settings().connector_dropbox_enabled:  # needs the owner's Dropbox app (A7b)
    REGISTRY["dropbox"] = DropboxConnector()
if get_settings().connector_rest_enabled:  # C2: generic REST/JSON APIs, credentials per source
    REGISTRY["rest"] = RestConnector()
