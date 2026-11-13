import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AuthForm } from "@/components/auth-form";
import { validateAuth } from "@/lib/auth-validation";

describe("validateAuth", () => {
  const ok = { email: "a@b.co", password: "longenough", confirm: "longenough" };

  it("accepts valid input", () => {
    expect(validateAuth(ok, "register")).toEqual({});
    expect(validateAuth({ ...ok, password: "x", confirm: "" }, "login")).toEqual({});
  });

  it("flags missing and malformed email", () => {
    expect(validateAuth({ ...ok, email: " " }, "login").email).toMatch(/enter your email/i);
    expect(validateAuth({ ...ok, email: "no-at-sign" }, "login").email).toMatch(/valid email/i);
  });

  it("enforces password length and confirmation only on register", () => {
    const short = { ...ok, password: "short", confirm: "short" };
    expect(validateAuth(short, "register").password).toMatch(/at least 8/i);
    expect(validateAuth(short, "login").password).toBeUndefined();
    expect(validateAuth({ ...ok, confirm: "different" }, "register").confirm).toMatch(/don't match/i);
  });
});

describe("AuthForm", () => {
  it("shows field errors and does not call the API when invalid", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthForm mode="register" />);
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Enter your email address.")).toBeInTheDocument();
    expect(screen.getByLabelText("Work email")).toHaveAttribute("aria-invalid", "true");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the backend message when sign-in is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { code: "invalid_credentials", message: "Incorrect email or password.", request_id: "r" } }), { status: 401 }),
      ),
    );
    render(<AuthForm mode="login" />);
    await userEvent.type(screen.getByLabelText("Work email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
  });
});
