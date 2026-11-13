export type AuthMode = "login" | "register";
export type AuthValues = { email: string; password: string; confirm: string };
export type AuthErrors = Partial<Record<keyof AuthValues, string>>;

export const MIN_PASSWORD = 8;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateAuth(values: AuthValues, mode: AuthMode): AuthErrors {
  const errors: AuthErrors = {};
  const email = values.email.trim();
  if (!email) errors.email = "Enter your email address.";
  else if (!EMAIL.test(email)) errors.email = "Enter a valid email address, like name@company.com.";

  if (!values.password) errors.password = "Enter your password.";
  else if (mode === "register" && values.password.length < MIN_PASSWORD)
    errors.password = `Use at least ${MIN_PASSWORD} characters.`;

  if (mode === "register" && values.confirm !== values.password)
    errors.confirm = "Passwords don't match.";
  return errors;
}
