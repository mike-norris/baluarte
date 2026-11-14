export interface LifecycleProps {
  /** The states in order, such as Draft, Submitted, Approved, Applied. */
  states: readonly string[];
  /** The current state; earlier states show as done. */
  current: string;
  /** Accessible name for the list. */
  label?: string;
}

/** Where an item is in its review and apply lifecycle, as Tabler steps. */
export function Lifecycle({ states, current, label = "Progress" }: LifecycleProps) {
  const at = states.indexOf(current);
  return (
    <ol className="steps steps-counter my-3" aria-label={label}>
      {states.map((state, index) => (
        <li
          key={state}
          className={`step-item ${index === at ? "active" : ""}`}
          aria-current={index === at ? "step" : undefined}
        >
          {state}
          {index < at && <span className="visually-hidden"> (done)</span>}
        </li>
      ))}
    </ol>
  );
}
