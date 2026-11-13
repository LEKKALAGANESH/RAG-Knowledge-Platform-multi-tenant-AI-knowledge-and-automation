import { BrandMark } from "@/components/brand-mark";

export default function AuthLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <aside className="hidden flex-col justify-between border-r bg-sidebar p-12 lg:flex">
        <BrandMark />
        <div className="max-w-md">
          <h2 className="text-2xl leading-snug font-semibold tracking-tight">Answers from your own knowledge, with sources.</h2>
          <p className="mt-4 leading-relaxed text-muted-foreground">
            Connect documents, drives, sites and databases into knowledge bases. Every answer links back to the
            passages it came from, and actions run only after approval.
          </p>
        </div>
        <p className="text-sm text-muted-foreground">Answers stay inside your organization.</p>
      </aside>
      <main className="flex flex-col px-4 py-10 sm:px-6">
        <div className="lg:hidden">
          <BrandMark />
        </div>
        <div className="m-auto w-full max-w-sm py-10">{children}</div>
      </main>
    </div>
  );
}
