import { useId } from "react";

import { Button } from "@/components/ui";
import type { GuidedRespondAction, InspectAndConfirmPayload } from "@/types/guided";

interface InspectAndConfirmTurnProps {
  payload: InspectAndConfirmPayload;
  onSubmit: (body: GuidedRespondAction) => void;
  disabled?: boolean;
  isTutorial?: boolean;
}

/** Inspection records facts about unchanged source bytes. Renaming or removing
 * columns is a processing request for the Composer, not a factual correction. */
export function InspectAndConfirmTurn({
  payload,
  onSubmit,
  disabled = false,
}: InspectAndConfirmTurnProps) {
  const warningsId = useId();

  function handleLooksRight() {
    if (disabled) return;
    onSubmit({
      chosen: null,
      edited_values: { columns: payload.observed.columns },
      custom_inputs: null,
      proposal_id: null,
      draft_hash: null,
      edit_target: null,
      control_signal: null,
    });
  }

  return (
    <div className="guided-turn guided-inspect-turn">
      <table className="guided-inspect-table">
        <thead>
          <tr>
            {payload.observed.columns.map((col) => (
              <th key={col} className="guided-inspect-th" scope="col">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {payload.observed.samples.map((sample, rowIndex) => (
            <tr key={rowIndex} className="guided-inspect-tr">
              {payload.observed.columns.map((col) => (
                <td key={col} className="guided-inspect-td">
                  {String(sample[col] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {payload.observed.samples.length === 0 && (
        <p className="guided-inspect-empty-note">
          Row contents aren&apos;t previewed here — confirm the column names.
        </p>
      )}
      <p className="guided-inspect-empty-note">
        These are the columns in your source. To rename or remove columns, ask the composer
        for a processing step in chat.
      </p>
      {payload.observed.warnings.length > 0 && (
        <aside id={warningsId} className="guided-inspect-warnings" aria-label="Data warnings">
          <ul className="guided-inspect-warnings-list">
            {payload.observed.warnings.map((warning, i) => (
              <li key={i} className="guided-inspect-warning-item">{warning}</li>
            ))}
          </ul>
        </aside>
      )}
      <div className="guided-inspect-actions">
        <Button variant="bare" className="guided-inspect-confirm-btn" onClick={handleLooksRight} disabled={disabled}>
          Looks right
        </Button>
      </div>
    </div>
  );
}
