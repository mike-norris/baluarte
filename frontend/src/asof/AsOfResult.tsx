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
      <p role="alert" className="alert alert-danger text-body mb-0">
        {`The server did not confirm these results are as of ${when}; they may be current. Go back to now, or ask an administrator to update Baluarte.`}
      </p>
    );
  }
  const run = answer.evaluation_run;
  return (
    <p className="alert alert-warning text-body mb-0">
      {run
        ? `As of ${when}: from evaluation run ${String(run.id)}, finished ${formatTime(run.finished_at)}, the latest finished by then.`
        : `No evaluation had finished by ${when}, so there are no results for then.`}
    </p>
  );
}
