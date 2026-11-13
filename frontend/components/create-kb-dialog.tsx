"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { KnowledgeBase } from "@/lib/api/types";

const MAX_NAME = 200; // backend KBIn.name max_length

export function CreateKbDialog({ trigger, onCreated }: { trigger: ReactNode; onCreated: (kb: KnowledgeBase) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  function reset(next: boolean) {
    setOpen(next);
    if (!next) {
      setName("");
      setError(null);
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return setError("Give the knowledge base a name.");
    if (trimmed.length > MAX_NAME) return setError(`Keep the name under ${MAX_NAME} characters.`);
    setPending(true);
    setError(null);
    try {
      const kb = await api.createKb({ name: trimmed });
      reset(false);
      onCreated(kb);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError("Your role in this organization can't create knowledge bases. Ask an owner or admin for editor access.");
      } else {
        setError(err instanceof ApiError ? err.message : "Couldn't create the knowledge base. Try again.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <form onSubmit={onSubmit} noValidate className="grid gap-5">
          <DialogHeader>
            <DialogTitle>Create knowledge base</DialogTitle>
            <DialogDescription>Group related documents and sources so they can be searched together.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="kb-name">Name</Label>
            <Input
              id="kb-name"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setError(null);
              }}
              placeholder="e.g. Finance policies"
              maxLength={MAX_NAME + 1}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? "kb-error" : undefined}
              autoFocus
            />
          </div>
          {error && (
            <p id="kb-error" role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => reset(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending}>
              {pending && <Loader2 className="animate-spin" aria-hidden />}
              Create knowledge base
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
