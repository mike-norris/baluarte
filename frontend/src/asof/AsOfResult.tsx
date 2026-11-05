import { formatTime } from "../time";
import { useAsOf } from "./context";
import { asOfText, type AsOfAnswer } from "./format";

/**
 * Which evaluation run a past-date answer came from. When the server doesn't confirm the date,
 * that is said plainly: the results may then be current, and must not pass for the past.
 */
export function AsOfResult({ answer }: { answer: AsOfAnswer | undefined }) {
  const asOf = useAsOf();
  if (asOf === null || answer === undefined) return null;
  const when = asOfText(asOf);
  if (answer.as_of === undefined || answer.as_of === null || !("evaluation_run" in answer)) {
    return (
      <p role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
        {`The server did not confirm these results are as of ${when}; they may be current. Go back to now, or ask an administrator to update Baluarte.`}
      </p>
    );
  }
  const run = answer.evaluation_run;
  return (
    <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
      {run
        ? `As of ${when}: from evaluation run ${String(run.id)}, finished ${formatTime(run.finished_at)}, the latest finished by then.`
        : `No evaluation had finished by ${when}, so there are no results for then.`}
    </p>
  );
}
