import { useId, useState, type FormEvent } from "react";

import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { useAsOfState } from "./context";
import { asOfProblem, asOfText, fromLocal, nowLocal, toLocal } from "./format";

const toggleClass =
  "rounded border px-2 py-1 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600";

/** The header control that chooses the date pages are viewed as of. */
export function AsOfPicker() {
  const { chosen, choose } = useAsOfState();
  const [open, setOpen] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const formId = useId();
  const errorId = useId();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = new FormData(event.currentTarget).get("as_of");
    const text = typeof value === "string" ? value : "";
    const reason = asOfProblem(text);
    if (reason) {
      setProblem(reason);
      return;
    }
    setProblem(null);
    setOpen(false);
    choose(fromLocal(text));
  }

  return (
    <div className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={formId}
        onClick={() => {
          setOpen((value) => !value);
          setProblem(null);
        }}
        className={`${toggleClass} ${
          chosen
            ? "border-amber-400 bg-amber-50 text-amber-950"
            : "border-slate-300 bg-white text-slate-700 hover:bg-slate-100"
        }`}
      >
        {chosen ? `Viewing as of ${asOfText(chosen)}` : "Viewing now"}
      </button>
      {open && (
        <form
          id={formId}
          onSubmit={apply}
          noValidate
          aria-label="View as of a past date"
          className="absolute right-0 z-10 mt-2 w-72 space-y-3 rounded border border-slate-200 bg-white p-3 shadow-lg"
        >
          <label className="block text-sm font-medium">
            View as of (UTC)
            <input
              name="as_of"
              type="datetime-local"
              defaultValue={chosen ? toLocal(chosen) : ""}
              max={nowLocal()}
              aria-invalid={problem !== null}
              aria-describedby={problem ? errorId : undefined}
              className={fieldClass}
            />
          </label>
          {problem && (
            <p id={errorId} role="alert" className="text-sm text-red-800">
              {problem}
            </p>
          )}
          <p className="text-xs text-slate-600">
            Findings, controls, the dashboard, projects and changes are shown from the latest
            evaluation finished by then.
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="submit" className={buttonClass}>
              View
            </button>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
              }}
              className={secondaryClass}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
