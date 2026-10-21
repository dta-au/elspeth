import { useEffect, useId, useRef, useState } from "react";
import { Button, Icon, Input } from "@/components/ui";

interface GraphChecksumProps {
  contentHash: string | undefined;
  stateIdentity: string;
}

type Feedback =
  | "idle"
  | "copying"
  | "copied"
  | "reading"
  | "match"
  | "mismatch"
  | "empty"
  | "invalid"
  | "read-denied"
  | "read-unavailable"
  | "read-failed"
  | "copy-denied"
  | "copy-unavailable"
  | "copy-failed";

const FEEDBACK_TEXT: Record<Feedback, string> = {
  idle: "",
  copying: "Copying checksum…",
  copied: "Checksum copied.",
  reading: "Reading clipboard…",
  match: "Checksum matches this graph.",
  mismatch: "Checksum does not match this graph.",
  empty: "No checksum to compare. Copy a checksum or use Paste checksum.",
  invalid: "Invalid checksum. Enter exactly 64 hexadecimal characters.",
  "read-denied": "Clipboard access denied. Use Paste checksum to compare manually.",
  "read-unavailable": "Clipboard reading is unavailable. Use Paste checksum to compare manually.",
  "read-failed": "Could not read the clipboard. Use Paste checksum to compare manually.",
  "copy-denied": "Clipboard access denied. Select the checksum above and copy it manually.",
  "copy-unavailable": "Clipboard copying is unavailable. Select the checksum above and copy it manually.",
  "copy-failed": "Could not copy the checksum. Select it above and copy it manually.",
};

function accessDenied(error: unknown): boolean {
  return (error instanceof DOMException || error instanceof Error)
    && (error.name === "NotAllowedError" || error.name === "SecurityError");
}

/** Consume the server digest: redacted browser content cannot reproduce it. */
export function GraphChecksum({ contentHash, stateIdentity }: GraphChecksumProps) {
  // A fresh instance clears feedback and invalidates pending operations even
  // for A → B → A, or a new saved state whose content hash is unchanged.
  return (
    <GraphChecksumControls
      key={JSON.stringify([stateIdentity, contentHash])}
      contentHash={contentHash}
    />
  );
}

function GraphChecksumControls({ contentHash }: Pick<GraphChecksumProps, "contentHash">) {
  const titleId = useId();
  const [feedback, setFeedback] = useState<Feedback>("idle");
  const [showPaste, setShowPaste] = useState(false);
  const [pastedChecksum, setPastedChecksum] = useState("");
  const operation = useRef(0);

  useEffect(() => () => { operation.current += 1; }, []);

  const busy = feedback === "reading" || feedback === "copying";

  function compare(value: string) {
    const checksum = value.trim();
    if (checksum.length === 0) {
      setFeedback("empty");
    } else if (!/^[a-fA-F0-9]{64}$/.test(checksum)) {
      setFeedback("invalid");
    } else {
      setFeedback(checksum.toLowerCase() === contentHash ? "match" : "mismatch");
    }
  }

  async function copyChecksum() {
    if (contentHash === undefined) return;
    const currentOperation = ++operation.current;
    if (typeof navigator.clipboard?.writeText !== "function") {
      setFeedback("copy-unavailable");
      return;
    }
    setFeedback("copying");
    try {
      await navigator.clipboard.writeText(contentHash);
      if (operation.current === currentOperation) setFeedback("copied");
    } catch (error) {
      if (operation.current === currentOperation) {
        setFeedback(accessDenied(error) ? "copy-denied" : "copy-failed");
      }
    }
  }

  async function matchClipboard() {
    const currentOperation = ++operation.current;
    if (typeof navigator.clipboard?.readText !== "function") {
      setFeedback("read-unavailable");
      return;
    }
    setFeedback("reading");
    try {
      const value = await navigator.clipboard.readText();
      if (operation.current === currentOperation) compare(value);
    } catch (error) {
      if (operation.current === currentOperation) {
        setFeedback(accessDenied(error) ? "read-denied" : "read-failed");
      }
    }
  }

  return (
    <section className="graph-checksum" aria-labelledby={titleId}>
      <div className="graph-checksum-heading">
        <span id={titleId} className="graph-checksum-title">Graph checksum</span>
        <span className="graph-checksum-algorithm">SHA-256</span>
      </div>
      {contentHash === undefined ? (
        <p className="graph-checksum-note">Checksum unavailable for this graph.</p>
      ) : (
        <>
          <code className="graph-checksum-value" aria-label="Graph content checksum">{contentHash}</code>
          <div className="graph-checksum-actions">
            <Button compact disabled={busy} onClick={() => { void copyChecksum(); }}>Copy checksum</Button>
            <Button compact disabled={busy} onClick={() => { void matchClipboard(); }}>Match clipboard</Button>
            <Button
              compact
              aria-expanded={showPaste}
              onClick={() => {
                operation.current += 1;
                setFeedback("idle");
                setShowPaste((shown) => !shown);
              }}
            >
              Paste checksum
            </Button>
          </div>
          {showPaste && (
            <form
              className="graph-checksum-paste"
              onSubmit={(event) => {
                event.preventDefault();
                operation.current += 1;
                compare(pastedChecksum);
              }}
            >
              <Input
                label="Checksum to compare"
                hint="Paste a SHA-256 checksum here. Surrounding spaces and letter case are ignored."
                mono
                autoFocus
                autoComplete="off"
                spellCheck={false}
                value={pastedChecksum}
                onChange={(event) => {
                  operation.current += 1;
                  setFeedback("idle");
                  setPastedChecksum(event.target.value);
                }}
              />
              <Button compact type="submit">Compare checksum</Button>
            </form>
          )}
          <div role="status" aria-atomic="true" className={`graph-checksum-feedback graph-checksum-feedback-${feedback}`}>
            {(feedback === "match" || feedback === "mismatch") && (
              <Icon name={feedback === "match" ? "check" : "cross"} />
            )}
            {FEEDBACK_TEXT[feedback]}
          </div>
          <details className="graph-checksum-scope">
            <summary>What this checksum covers</summary>
            <p>
              Authored sources, steps, connections, outputs and pipeline metadata,
              including names, descriptions and options. Source order and fan-in
              branch order are significant. Excludes saved version, session identity,
              timestamps, validation results, Composer bookkeeping and local graph layout.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
