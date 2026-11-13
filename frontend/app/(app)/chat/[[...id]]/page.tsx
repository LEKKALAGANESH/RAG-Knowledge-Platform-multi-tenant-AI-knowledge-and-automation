import type { Metadata } from "next";
import { Suspense } from "react";
import { Chat, ChatSkeleton } from "@/components/chat";

export const metadata: Metadata = { title: "Chat" };

async function ChatRoute({ params }: { params: Promise<{ id?: string[] }> }) {
  const id = (await params).id?.[0] ?? null;
  return <Chat key={id ?? "new"} conversationId={id} />;
}

export default function ChatPage({ params }: PageProps<"/chat/[[...id]]">) {
  return (
    <Suspense fallback={<ChatSkeleton />}>
      <ChatRoute params={params} />
    </Suspense>
  );
}
