import { IconHistory } from "@tabler/icons-react";
import { useId, useState, type FormEvent } from "react";

import { useDropdown } from "../ui/useDropdown";
import { useAsOfState } from "./context";
import { asOfProblem, asOfText, fromLocal, nowLocal, toLocal } from "./format";

/** The header control that chooses the date pages are viewed as of. */
export function AsOfPicker() {
  const { chosen, choose } = useAsOfState();
  const { open, setOpen, root, trigger } = useDropdown<HTMLDivElement, HTMLButtonElement>();
  const [problem, setProblem] = useState<string | null>(null);
  const formId = useId();
  const inputId = useId();
  const errorId = useId();
  const label = chosen ? `Viewing as of ${asOfText(chosen)}` : "Viewing now";

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
    <div ref={root} className="dropdown">
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={formId}
        onClick={() => {
          setOpen((value) => !value);
          setProblem(null);
        }}
        className={`btn ${chosen ? "bg-warning-subtle text-warning-emphasis border-warning-subtle" : ""}`}
      >
        <IconHistory size={18} className="icon" aria-hidden="true" />
        <span className="d-none d-sm-inline">{label}</span>
      </button>
      {open && (
        <form
          id={formId}
          onSubmit={apply}
          noValidate
          aria-label="View as of a past date"
          className="dropdown-menu dropdown-menu-end show p-3"
          style={{ width: "18rem", right: 0, left: "auto" }}
        >
          <div className="mb-3">
            <label htmlFor={inputId} className="form-label">
              View as of (UTC)
            </label>
            <input
              id={inputId}
              name="as_of"
              type="datetime-local"
              defaultValue={chosen ? toLocal(chosen) : ""}
              max={nowLocal()}
              aria-invalid={problem !== null}
              aria-describedby={problem ? errorId : undefined}
              className={`form-control ${problem ? "is-invalid" : ""}`}
            />
            {problem && (
              <div id={errorId} role="alert" className="invalid-feedback d-block">
                {problem}
              </div>
            )}
          </div>
          <p className="form-hint mb-3">
            Findings, controls, the dashboard, projects and changes are shown from the latest
            evaluation finished by then.
          </p>
          <div className="btn-list">
            <button type="submit" className="btn btn-primary">
              View
            </button>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
              }}
              className="btn"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
