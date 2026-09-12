export type OrgRole = "OWNER" | "ADMIN" | "MEMBER" | "VIEWER";

export const ROLE_HIERARCHY: Record<OrgRole, number> = {
  VIEWER: 0,
  MEMBER: 1,
  ADMIN: 2,
  OWNER: 3,
};

export type User = {
  id: string;
  email: string;
  full_name: string | null;
  created_at: string;
};

export type Organization = {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  role: OrgRole;
};

export type Member = {
  id: string;
  user_id: string;
  email: string;
  full_name: string | null;
  role: OrgRole;
  created_at: string;
};

export type DocumentStatus = "UPLOADING" | "PROCESSING" | "READY" | "FAILED" | "DELETED";

export type Document = {
  id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  status: DocumentStatus;
  uploaded_by: string | null;
  created_at: string;
  processing_started_at: string | null;
  processing_completed_at: string | null;
  chunk_count: number;
  embedding_model: string | null;
  failure_reason: string | null;
  retry_count: number;
};

export type DocumentListResponse = {
  items: Document[];
  total: number;
  page: number;
  page_size: number;
};

export type MessageRole = "USER" | "ASSISTANT";

export type Citation = {
  id: string;
  document_id: string;
  document_name: string;
  chunk_id: string;
  page: number | null;
  section: string | null;
  excerpt: string;
};

export type ChatMessage = {
  id: string;
  role: MessageRole;
  content: string;
  citations: Citation[] | null;
  chunks_considered: number | null;
  chunks_used: number | null;
  created_at: string;
};

export type ChatResponse = {
  conversation_id: string;
  message_id: string;
  answer: string;
  citations: Citation[];
  retrieval: {
    chunks_considered: number;
    chunks_used: number;
  };
};

export type Conversation = {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
};

export type ConversationDetail = Conversation & {
  messages: ChatMessage[];
};

export type ConversationListResponse = {
  items: Conversation[];
  total: number;
  page: number;
  page_size: number;
};
