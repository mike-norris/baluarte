import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";

import {
  attest,
  decideControlException,
  getBackups,
  getCatalogue,
  getPosture,
  listControlExceptions,
  listServices,
  type Catalogue,
  type CatalogueControl,
  type ControlException,
  type ControlPosture,
  type FrameworkId,
  type Posture,
} from "../api/compliance";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { ExceptionRequestForm, requestedMessage } from "../compliance/ExceptionRequestForm";
import { BackupsPanel, ServicesSection } from "../compliance/Services";
import {
  frameworkList,
  frameworks,
  frameworkShort,
  statusClass,
  statusLabel,
  statusOrder,
} from "../compliance/format";
import { asApiError, cardClass, errorClass, Problems, Values } from "../settings/parts";
import { formatTime } from "../time";

const cell = "px-3 py-2 align-top";

function StatusBadge({ status }: { status: ControlPosture["status"] }) {
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${statusClass[status]}`}
    >
      {statusLabel[status]}
    </span>
  );
}

/** "SOC 2 CC6.1 · ISO 27001 A.8.5", for the selected frameworks (or all when none is selected). */
function references(control: CatalogueControl | undefined, selected: FrameworkId[]): string {
  if (!control) return "";
  const shown = selected.length > 0 ? selected : frameworks.map((item) => item.id);
  return shown
    .map((id) => {
      const refs = control.mappings[id];
      return refs && refs.length > 0 ? `${frameworkShort(id)} ${refs.join(", ")}` : null;
    })
    .filter(Boolean)
    .join(" · ");
}

function evidenceRows(evidence: Record<string, unknown>): [string, ReactNode][] {
  return Object.entries(evidence).map(([key, value]) => [
    key.replaceAll("_", " "),
    typeof value === "string" ? value : JSON.stringify(value),
  ]);
}

function AttestForm({
  control,
  csrf,
  onDone,
}: {
  control: CatalogueControl;
  csrf: string;
  onDone: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [statement, setStatement] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const id = useId();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = statement.trim();
    if (text.length < 10 || text.length > 2000) {
      setProblems(["Write a statement of 10 to 2,000 characters."]);
      return;
    }
    setProblems([]);
    setSending(true);
    try {
      const result = await attest({ control_id: control.id, statement: text }, csrf);
      setOpen(false);
      setStatement("");
      onDone(`Attested "${control.title}" until ${formatTime(result.expires_at)}.`);
    } catch (error) {
      setProblems([asApiError(error).message]);
    } finally {
      setSending(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
        className={secondaryClass}
      >
        Attest
        <span className="sr-only"> {control.title}</span>
      </button>
    );
  }
  return (
    <form
      onSubmit={(event) => void submit(event)}
      noValidate
      aria-label={`Attest ${control.title}`}
      className="space-y-2"
    >
      <Problems messages={problems} />
      <label htmlFor={`${id}-statement`} className="block text-sm font-medium">
        Statement
      </label>
      <textarea
        id={`${id}-statement`}
        aria-describedby={`${id}-statement-hint`}
        value={statement}
        onChange={(event) => {
          setStatement(event.currentTarget.value);
        }}
        rows={3}
        maxLength={2000}
        className={fieldClass}
      />
      <p id={`${id}-statement-hint`} className="text-xs text-slate-600">
        What is in place and where the evidence is kept. Recorded under your name
        {control.attestation_days
          ? ` and valid for ${String(control.attestation_days)} days.`
          : "."}
      </p>
      <div className="flex gap-2">
        <button type="submit" disabled={sending} className={buttonClass}>
          {sending ? "Recording…" : "Record attestation"}
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
  );
}

function ControlItem({
  posture,
  control,
  selected,
  csrf,
  onChanged,
}: {
  posture: ControlPosture;
  control: CatalogueControl | undefined;
  selected: FrameworkId[];
  /** Null for readers who cannot attest. */
  csrf: string | null;
  onChanged: (message: string) => void;
}) {
  const refs = references(control, selected);
  return (
    <li className="space-y-2 border-b border-slate-200 py-3 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <StatusBadge status={posture.status} />
        <h3 className="font-medium">{posture.title}</h3>
        {refs && <span className="text-xs text-slate-600">{refs}</span>}
      </div>
      <p className="text-sm text-slate-700">{posture.detail}</p>
      <details className="text-sm">
        <summary className="cursor-pointer font-medium text-sky-800">
          Details and how to fix
          <span className="sr-only">: {posture.title}</span>
        </summary>
        <div className="mt-2 space-y-3">
          {control?.description && <p className="text-slate-700">{control.description}</p>}
          {control?.how_to && (
            <div>
              <p className="font-medium">How to meet it</p>
              <p className="whitespace-pre-line text-slate-700">{control.how_to}</p>
            </div>
          )}
          {Object.keys(posture.evidence).length > 0 && (
            <div>
              <p className="font-medium">Evidence</p>
              <Values rows={evidenceRows(posture.evidence)} />
            </div>
          )}
          {posture.exception && (
            <p>
              {`Exception ${String(posture.exception.id)} is in force until ${posture.exception.expires_on}.`}
            </p>
          )}
          {posture.attestation && (
            <p>
              {`Attested by ${posture.attestation.attested_by} on ${formatTime(posture.attestation.attested_at)}; valid until ${formatTime(posture.attestation.expires_at)}.`}
            </p>
          )}
          {csrf && control && control.attestation_days !== null && (
            <AttestForm control={control} csrf={csrf} onDone={onChanged} />
          )}
        </div>
      </details>
    </li>
  );
}

function exceptionState(exception: ControlException): string {
  if (exception.in_force) return "In force";
  if (exception.state === "approved") return "Expired";
  return {
    proposed: "Awaiting approval",
    rejected: "Rejected",
    revoked: "Revoked",
  }[exception.state];
}

function ExceptionRow({
  exception,
  title,
  me,
  csrf,
  onChanged,
}: {
  exception: ControlException;
  title: string;
  me: string;
  csrf: string | null;
  onChanged: (message: string) => void;
}) {
  const [failure, setFailure] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [note, setNote] = useState("");
  const noteId = useId();
  const own = exception.requested_by === me;

  async function decide(decision: "approve" | "reject" | "revoke") {
    if (!csrf) return;
    setFailure(null);
    if (decision === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    try {
      await decideControlException(exception.id, decision, note.trim() || null, csrf);
      const done = {
        approve: "approved",
        reject: "rejected",
        revoke: "revoked",
      }[decision];
      onChanged(`Exception ${String(exception.id)} ${done}.`);
    } catch (error) {
      setFailure(asApiError(error).message);
    }
  }

  let actions: ReactNode = null;
  if (csrf && exception.state === "proposed") {
    actions = own ? (
      <span className="text-xs text-slate-600">Another administrator must decide.</span>
    ) : (
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-64 flex-1">
          <label htmlFor={noteId} className="text-sm font-medium">
            Note (required to reject)
          </label>
          <textarea
            id={noteId}
            value={note}
            maxLength={2000}
            rows={2}
            onChange={(event) => {
              setNote(event.target.value);
            }}
            className={fieldClass}
          />
        </div>
        <button type="button" onClick={() => void decide("approve")} className={secondaryClass}>
          Approve<span className="sr-only"> exception {exception.id}</span>
        </button>
        <button type="button" onClick={() => void decide("reject")} className={secondaryClass}>
          Reject<span className="sr-only"> exception {exception.id}</span>
        </button>
      </div>
    );
  } else if (csrf && exception.in_force) {
    actions = confirmRevoke ? (
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => void decide("revoke")} className={buttonClass}>
          Confirm revoke
        </button>
        <button
          type="button"
          onClick={() => {
            setConfirmRevoke(false);
          }}
          className={secondaryClass}
        >
          Cancel
        </button>
      </div>
    ) : (
      <button
        type="button"
        onClick={() => {
          setConfirmRevoke(true);
        }}
        className={secondaryClass}
      >
        Revoke<span className="sr-only"> exception {exception.id}</span>
      </button>
    );
  }

  return (
    <tr>
      <th scope="row" className={`${cell} font-medium`}>
        {title}
      </th>
      <td className={cell}>{exceptionState(exception)}</td>
      <td className={cell}>{exception.justification}</td>
      <td className={`${cell} whitespace-nowrap`}>{exception.expires_on}</td>
      <td className={cell}>
        {`${exception.requested_by}, ${formatTime(exception.requested_at)}`}
        {exception.decided_by &&
          (exception.self_approved
            ? " (not reviewed: review was off)"
            : `; decided by ${exception.decided_by}`)}
      </td>
      <td className={cell}>
        {actions}
        {failure && (
          <p role="alert" className="mt-1 text-xs text-red-800">
            {failure}
          </p>
        )}
      </td>
    </tr>
  );
}

function Exceptions({
  catalogue,
  exceptions,
  me,
  csrf,
  onChanged,
}: {
  catalogue: Catalogue;
  exceptions: ControlException[];
  me: string;
  csrf: string | null;
  onChanged: (message: string) => void;
}) {
  const [requesting, setRequesting] = useState(false);
  const title = (id: string) => catalogue.controls.find((item) => item.id === id)?.title ?? id;

  return (
    <section aria-labelledby="exceptions-heading" className={cardClass}>
      <h2 id="exceptions-heading" className="text-lg font-semibold">
        Exceptions
      </h2>
      <p className="text-sm text-slate-600">
        Time-limited permission to run below one control&apos;s requirement. Requests, decisions and
        revocations are audited.
      </p>
      {csrf &&
        (requesting ? (
          <ExceptionRequestForm
            controls={catalogue.controls.map(({ id, title: name }) => ({
              id,
              title: name,
            }))}
            csrf={csrf}
            onRequested={(exception) => {
              setRequesting(false);
              onChanged(requestedMessage(exception));
            }}
            onCancel={() => {
              setRequesting(false);
            }}
          />
        ) : (
          <button
            type="button"
            onClick={() => {
              setRequesting(true);
            }}
            className={secondaryClass}
          >
            Request an exception
          </button>
        ))}
      {exceptions.length === 0 ? (
        <p className="text-sm text-slate-700">No exceptions have been requested.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Exceptions to the compliance profile</caption>
            <thead className="border-b border-slate-200 text-xs text-slate-600 uppercase">
              <tr>
                <th scope="col" className={cell}>
                  Control
                </th>
                <th scope="col" className={cell}>
                  State
                </th>
                <th scope="col" className={cell}>
                  Justification
                </th>
                <th scope="col" className={cell}>
                  Expires
                </th>
                <th scope="col" className={cell}>
                  Requested
                </th>
                <th scope="col" className={cell}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {exceptions.map((exception) => (
                <ExceptionRow
                  key={exception.id}
                  exception={exception}
                  title={title(exception.control_id)}
                  me={me}
                  csrf={csrf}
                  onChanged={onChanged}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Summary({ posture }: { posture: Posture }) {
  return (
    <dl aria-label="Controls by status" className="flex flex-wrap gap-3">
      {statusOrder.map((status) => (
        <div key={status} className="rounded border border-slate-200 bg-white px-3 py-2">
          <dt className="text-xs text-slate-600">{statusLabel[status]}</dt>
          <dd className="text-xl font-semibold">{posture.summary[status]}</dd>
        </div>
      ))}
    </dl>
  );
}

function sorted(controls: ControlPosture[], catalogue: Catalogue): ControlPosture[] {
  const order = new Map(catalogue.controls.map((control, index) => [control.id, index]));
  return [...controls].sort(
    (a, b) =>
      statusOrder.indexOf(a.status) - statusOrder.indexOf(b.status) ||
      (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0),
  );
}

/** How Baluarte itself meets its operating profile. Admins and auditors. */
export function CompliancePage() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const allowed = user?.permissions.includes("compliance:read") ?? false;
  const admin = user?.permissions.includes("settings:manage") ?? false;
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const key = `#${String(version)}`;
  const posture = useLoad(`compliance-posture${key}`, getPosture);
  const catalogue = useLoad("compliance-catalogue", getCatalogue);
  const exceptions = useLoad(`compliance-exceptions${key}`, listControlExceptions);
  const services = useLoad(`compliance-services${key}`, listServices);
  const backups = useLoad(`compliance-backups${key}`, getBackups);

  const changed = (text: string) => {
    setMessage(text);
    setVersion((value) => value + 1);
  };

  let body: ReactNode;
  const error = posture.error ?? catalogue.error ?? exceptions.error;
  if (!allowed) {
    body = (
      <p role="alert" className={errorClass}>
        Only administrators and auditors can view Baluarte&apos;s compliance posture.
      </p>
    );
  } else if (error) {
    body = (
      <ErrorMessage
        error={error}
        retry={() => {
          posture.retry();
          catalogue.retry();
          exceptions.retry();
        }}
      />
    );
  } else if (!posture.data || !catalogue.data || !exceptions.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading the compliance posture…
      </p>
    );
  } else {
    const data = posture.data;
    const controls = catalogue.data;
    const csrf = admin && user ? user.csrf_token : null;
    body = (
      <>
        {!data.configured && (
          <div role="alert" className="rounded border border-amber-300 bg-amber-50 p-3 text-sm">
            <p className="font-medium text-amber-950">No compliance profile has been chosen.</p>
            <p className="text-amber-950">
              {admin ? (
                <>
                  Choose the frameworks Baluarte must meet in{" "}
                  <Link to="/settings#compliance" className={linkClass}>
                    Settings
                  </Link>
                  . Until then nothing is enforced, and the controls below are shown for reference.
                </>
              ) : (
                "Ask a Baluarte administrator to choose one. Until then nothing is enforced."
              )}
            </p>
          </div>
        )}
        <section aria-labelledby="posture-heading" className="space-y-3">
          <h2 id="posture-heading" className="text-lg font-semibold">
            Posture
          </h2>
          <p className="text-sm text-slate-600">
            {`Frameworks: ${frameworkList(data.frameworks)}. Evaluated ${formatTime(data.evaluated_at)}.`}
            {admin && data.configured && (
              <>
                {" "}
                <Link to="/settings#compliance" className={linkClass}>
                  Change the profile
                </Link>
              </>
            )}
          </p>
          <Summary posture={data} />
          <ul aria-label="Controls" className={`${cardClass} space-y-0`}>
            {sorted(data.controls, controls).map((item) => (
              <ControlItem
                key={item.id}
                posture={item}
                control={controls.controls.find((control) => control.id === item.id)}
                selected={data.frameworks}
                csrf={csrf}
                onChanged={changed}
              />
            ))}
          </ul>
        </section>
        {services.error ? (
          <ErrorMessage error={services.error} retry={services.retry} />
        ) : services.data ? (
          <ServicesSection services={services.data} />
        ) : (
          <p role="status" className="text-sm text-slate-600">
            Loading services…
          </p>
        )}
        {backups.error ? (
          <ErrorMessage error={backups.error} retry={backups.retry} />
        ) : backups.data ? (
          <BackupsPanel backups={backups.data} />
        ) : (
          <p role="status" className="text-sm text-slate-600">
            Loading backups…
          </p>
        )}
        <Exceptions
          catalogue={controls}
          exceptions={exceptions.data}
          // In development mode the requester may decide their own exception.
          me={user && user.development_mode !== true ? `user:${String(user.user_id)}` : ""}
          csrf={csrf}
          onChanged={changed}
        />
      </>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Compliance
        </h1>
        <p className="mt-2 text-slate-600">
          How Baluarte itself meets the frameworks in its operating profile: retention, sessions,
          change review, encryption and the attestations an administrator records. Findings, by
          contrast, check your GitLab projects.
        </p>
      </div>
      <p role="status" className="text-sm text-emerald-800">
        {message ?? ""}
      </p>
      {body}
    </section>
  );
}
