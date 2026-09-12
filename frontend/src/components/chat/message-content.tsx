"use client";

import { Fragment } from "react";
import { BookOpenCheck } from "lucide-react";

import type { Citation } from "@/lib/types";

const CITATION_TAG_RE = /\[SOURCE-\d+\]/g;

/** Splits assistant message text on [SOURCE-N] tags and renders each as a
 * distinct citation chip (not a plain hyperlink) so it reads as "evidence
 * marker", not "external link". Tags with no matching citation object
 * (should not happen - the backend only ever leaves valid tags in the
 * text, see app/rag/citations.py) render as plain text rather than a
 * broken/clickable chip. */
export function MessageContent({
  content,
  citations,
  onCitationClick,
}: {
  content: string;
  citations: Citation[];
  onCitationClick: (citation: Citation) => void;
}) {
  const byId = new Map(citations.map((c) => [c.id, c]));
  const parts = content.split(CITATION_TAG_RE);
  const tags = content.match(CITATION_TAG_RE) ?? [];

  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed">
      {parts.map((part, index) => {
        const tag = tags[index];
        const tagId = tag?.slice(1, -1); // "[SOURCE-1]" -> "SOURCE-1"
        const citation = tagId ? byId.get(tagId) : undefined;
        return (
          <Fragment key={index}>
            {part}
            {citation && (
              <button
                type="button"
                onClick={() => onCitationClick(citation)}
                className="mx-0.5 inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 align-middle text-xs font-medium text-primary hover:bg-primary/20"
              >
                <BookOpenCheck className="h-3 w-3" />
                {citation.id.replace("SOURCE-", "")}
              </button>
            )}
          </Fragment>
        );
      })}
    </p>
  );
}
