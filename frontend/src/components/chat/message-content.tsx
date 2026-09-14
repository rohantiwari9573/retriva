"use client";

import { Fragment } from "react";
import { BookOpenCheck } from "lucide-react";

import type { Citation } from "@/lib/types";

const CITATION_TAG_RE = /\[SOURCE-\d+\]/g;
// A conservative, safe-by-construction subset of markdown - bold, inline
// code, and "- "/"* " bullet lists - rendered as real React elements, never
// dangerouslySetInnerHTML. A full markdown library (react-markdown) was
// deliberately not added: its block-based AST doesn't have a natural place
// to interleave the existing [SOURCE-N] citation-chip splitting below
// without a custom remark plugin, which is real added complexity for a
// four-rule subset of formatting a local LLM's grounded-QA answers
// actually use in practice (see docs/rag.md's prompt template).
const INLINE_FORMAT_RE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

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
  const paragraphs = content.split(/\n{2,}/);

  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {paragraphs.map((paragraph, paragraphIndex) => (
        <Paragraph
          key={paragraphIndex}
          text={paragraph}
          byId={byId}
          onCitationClick={onCitationClick}
        />
      ))}
    </div>
  );
}

function Paragraph({
  text,
  byId,
  onCitationClick,
}: {
  text: string;
  byId: Map<string, Citation>;
  onCitationClick: (citation: Citation) => void;
}) {
  const lines = text.split("\n");
  const isBulletList = lines.length > 0 && lines.every((line) => /^\s*[-*]\s+/.test(line));

  if (isBulletList) {
    return (
      <ul className="list-disc space-y-1 pl-5">
        {lines.map((line, index) => (
          <li key={index}>
            <FormattedText text={line.replace(/^\s*[-*]\s+/, "")} byId={byId} onCitationClick={onCitationClick} />
          </li>
        ))}
      </ul>
    );
  }

  return (
    <p className="whitespace-pre-wrap">
      <FormattedText text={text} byId={byId} onCitationClick={onCitationClick} />
    </p>
  );
}

/** Renders one run of text, splitting first on citation tags (so a chip
 * never ends up nested inside a <strong>/<code> element) and then applying
 * bold/code formatting to the plain-text segments between them. */
function FormattedText({
  text,
  byId,
  onCitationClick,
}: {
  text: string;
  byId: Map<string, Citation>;
  onCitationClick: (citation: Citation) => void;
}) {
  const parts = text.split(CITATION_TAG_RE);
  const tags = text.match(CITATION_TAG_RE) ?? [];

  return (
    <>
      {parts.map((part, index) => {
        const tag = tags[index];
        const tagId = tag?.slice(1, -1); // "[SOURCE-1]" -> "SOURCE-1"
        const citation = tagId ? byId.get(tagId) : undefined;
        return (
          <Fragment key={index}>
            <InlineFormatted text={part} />
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
    </>
  );
}

function InlineFormatted({ text }: { text: string }) {
  const segments = text.split(INLINE_FORMAT_RE);
  return (
    <>
      {segments.map((segment, index) => {
        if (segment.startsWith("**") && segment.endsWith("**") && segment.length >= 4) {
          return <strong key={index}>{segment.slice(2, -2)}</strong>;
        }
        if (segment.startsWith("`") && segment.endsWith("`") && segment.length >= 2) {
          return (
            <code key={index} className="rounded bg-foreground/10 px-1 py-0.5 font-mono text-[0.85em]">
              {segment.slice(1, -1)}
            </code>
          );
        }
        return <Fragment key={index}>{segment}</Fragment>;
      })}
    </>
  );
}
