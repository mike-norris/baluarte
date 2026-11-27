import { useState, type FormEvent } from "react";

import { frameworkIds } from "../api/controls";
import {
  addEvidenceItem,
  deleteEvidenceItem,
  evidenceAction,
  fileHref,
  freshnessLabels,
  getEvidenceItem,
  listEvidenceItems,
  stateLabels,
  uploadEvidenceFile,
  type ControlInput,
  type EvidenceItem,
  type EvidenceKind,
  type EvidenceState,
  type Freshness,
} from "../api/evidenceItems";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabel } from "../controls/format";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";

const stateTones: Record<EvidenceState, Tone> = {
  draft: "secondary",
  submitted: "info",
  accepted: "success",
  rejected: "danger",
};

const freshnessTones: Record<Freshness, Tone> = {
  fresh: "success",
  stale: "warning",
  unknown: "secondary",
};

function usePermissions() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const permissions = user?.permissions ?? [];
  return {
    csrf: user?.csrf_token ?? "",
    userId: user?.user_id ?? null,
    canManage: permissions.includes("evidence:manage"),
    canReview: permissions.includes("evidence:review"),
    development: user?.development_mode === true,
  };
}

function message(failure: unknown): string {
  return failure instanceof Error ? failure.message : String(failure);
}

function person(ref: { display_name: string | null; user_id: number }): string {
  return ref.display_name ?? `user ${String(ref.user_id)}`;
}

function controlText(item: EvidenceItem): string {
  return item.controls
    .map((c) => `${frameworkLabel(c.framework)} ${c.control_id} (${c.version})`)
    .join(", ");
}

/** Add an evidence item as a draft: a file (uploaded next) or a reference. */
function AddForm({ onAdded }: { onAdded: (item: EvidenceItem) => void }) {
  const { csrf } = usePermissions();
  const [kind, setKind] = useState<EvidenceKind>("file");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [url, setUrl] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [framework, setFramework] = useState<string>("soc2");
  const [controlId, setControlId] = useState("");
  const [controls, setControls] = useState<ControlInput[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function addControl() {
    const id = controlId.trim();
    if (!id) return;
    if (!controls.some((c) => c.framework === framework && c.control_id === id)) {
      setControls([...controls, { framework, control_id: id }]);
    }
    setControlId("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const item = await addEvidenceItem(
        {
          kind,
          title: title.trim(),
          description: description.trim(),
          valid_until: validUntil || null,
          period_start: null,
          period_end: null,
          restricted,
          controls,
          ...(kind === "reference" ? { url: url.trim() } : {}),
        },
        csrf,
      );
      setTitle("");
      setDescription("");
      setUrl("");
      setValidUntil("");
      setControls([]);
      onAdded(item);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label="Add evidence"
      className="card card-body mb-3 stack-2"
      onSubmit={(event) => void submit(event)}
    >
      <h2 className="card-title">Add evidence</h2>
      <fieldset>
        <legend className="form-label">Kind</legend>
        {(["file", "reference"] as const).map((value) => (
          <label key={value} className="form-check form-check-inline">
            <input
              type="radio"
              className="form-check-input"
              name="kind"
              checked={kind === value}
              onChange={() => {
                setKind(value);
              }}
            />
            <span className="form-check-label">
              {value === "file" ? "A file to upload" : "A link to a document elsewhere"}
            </span>
          </label>
        ))}
      </fieldset>
      <label className="form-label">
        Title
        <input
          className="form-control mt-1"
          value={title}
          maxLength={200}
          onChange={(event) => {
            setTitle(event.target.value);
          }}
        />
      </label>
      <label className="form-label">
        Description
        <textarea
          className="form-control mt-1"
          rows={2}
          maxLength={4000}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </label>
      {kind === "reference" && (
        <label className="form-label">
          Address
          <input
            className="form-control mt-1"
            type="url"
            value={url}
            onChange={(event) => {
              setUrl(event.target.value);
            }}
          />
        </label>
      )}
      <label className="form-label">
        Fresh until
        <input
          className="form-control mt-1"
          type="date"
          value={validUntil}
          onChange={(event) => {
            setValidUntil(event.target.value);
          }}
        />
      </label>
      <fieldset>
        <legend className="form-label">Controls it supports</legend>
        <div className="d-flex flex-wrap gap-2 align-items-end">
          <label className="form-label mb-0">
            Framework
            <select
              className="form-select mt-1"
              value={framework}
              onChange={(event) => {
                setFramework(event.target.value);
              }}
            >
              {frameworkIds.map((id) => (
                <option key={id} value={id}>
                  {frameworkLabel(id)}
                </option>
              ))}
            </select>
          </label>
          <label className="form-label mb-0">
            Control
            <input
              className="form-control mt-1"
              value={controlId}
              maxLength={20}
              placeholder="CC8.1"
              onChange={(event) => {
                setControlId(event.target.value);
              }}
            />
          </label>
          <button type="button" className="btn" onClick={addControl}>
            Add control
          </button>
        </div>
        {controls.length > 0 && (
          <ul aria-label="Controls to link" className="list-inline mt-2 mb-0">
            {controls.map((c) => (
              <li key={`${c.framework}:${c.control_id}`} className="list-inline-item">
                <Badge tone="primary">{`${frameworkLabel(c.framework)} ${c.control_id}`}</Badge>
              </li>
            ))}
          </ul>
        )}
      </fieldset>
      <label className="form-check">
        <input
          type="checkbox"
          className="form-check-input"
          checked={restricted}
          onChange={(event) => {
            setRestricted(event.target.checked);
          }}
        />
        <span className="form-check-label">Restricted: only reviewers and I can see it</span>
      </label>
      {error && <p className="text-danger mb-0">{error}</p>}
      <div>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || title.trim().length < 3 || (kind === "reference" && !url.trim())}
        >
          Add as draft
        </button>
      </div>
    </form>
  );
}

/** One item: what it holds, its review, and what the viewer may do with it. */
function ItemDetail({
  item,
  checksEnabled,
  onChanged,
}: {
  item: EvidenceItem;
  checksEnabled: boolean;
  onChanged: (text: string, item: EvidenceItem | null) => void;
}) {
  const { csrf, userId, canManage, canReview, development } = usePermissions();
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const own = userId === item.added_by.user_id;
  const draft = item.state === "draft";

  async function run(task: () => Promise<EvidenceItem | null>, text: string) {
    setError(null);
    try {
      onChanged(text, await task());
    } catch (failure) {
      setError(message(failure));
    }
  }

  return (
    <section aria-labelledby={`item-${String(item.id)}`} className="card card-body mb-3">
      <h2 id={`item-${String(item.id)}`} className="card-title">
        {item.title}
      </h2>
      <div className="d-flex flex-wrap gap-2 mb-2">
        <Badge tone={stateTones[item.state]}>{stateLabels[item.state]}</Badge>
        <Badge tone={freshnessTones[item.freshness]}>{freshnessLabels[item.freshness]}</Badge>
        {item.restricted && <Badge tone="purple">Restricted</Badge>}
      </div>
      {item.description && <p>{item.description}</p>}
      <dl className="row mb-2">
        <dt className="col-sm-3">Controls</dt>
        <dd className="col-sm-9">{controlText(item) || "None yet"}</dd>
        <dt className="col-sm-3">Added by</dt>
        <dd className="col-sm-9">{`${person(item.added_by)}, ${formatTime(item.added_at)}`}</dd>
        <dt className="col-sm-3">Fresh until</dt>
        <dd className="col-sm-9">{item.valid_until ?? "Not set"}</dd>
        {item.kind === "file" ? (
          <>
            <dt className="col-sm-3">File</dt>
            <dd className="col-sm-9">
              {item.file_removed_at ? (
                `Removed by retention ${formatTime(item.file_removed_at)}; SHA-256 ${item.sha256 ?? ""}`
              ) : item.sha256 ? (
                <>
                  <a href={fileHref(item.id)}>{`Download ${item.filename ?? "file"}`}</a>
                  <div className="text-secondary small text-break">{`SHA-256 ${item.sha256}`}</div>
                </>
              ) : (
                "Not uploaded yet"
              )}
            </dd>
          </>
        ) : (
          <>
            <dt className="col-sm-3">Address</dt>
            <dd className="col-sm-9 text-break">{item.url}</dd>
            <dt className="col-sm-3">Last check</dt>
            <dd className="col-sm-9">
              {item.check_status
                ? `${item.check_status === "reachable" ? "Reachable" : "Could not be reached"}${item.check_http_status === null ? "" : ` (HTTP ${String(item.check_http_status)})`}, ${formatTime(item.checked_at)}`
                : "Not checked"}
            </dd>
          </>
        )}
        {item.reviewed_by && (
          <>
            <dt className="col-sm-3">Reviewed by</dt>
            <dd className="col-sm-9">
              {`${person(item.reviewed_by)}, ${formatTime(item.reviewed_at)}${item.review_note ? `: “${item.review_note}”` : ""}${item.development_review ? " (their own, in development mode)" : ""}`}
            </dd>
          </>
        )}
        {item.superseded_by_id !== null && (
          <>
            <dt className="col-sm-3">Superseded by</dt>
            <dd className="col-sm-9">{`Item ${String(item.superseded_by_id)}`}</dd>
          </>
        )}
      </dl>
      {error && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
      <div className="btn-list">
        {canManage && own && draft && item.kind === "file" && (
          <label className="btn">
            {item.sha256 ? "Replace file" : "Upload file"}
            <input
              type="file"
              className="visually-hidden"
              aria-label={`File for ${item.title}`}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file)
                  void run(() => uploadEvidenceFile(item.id, file, csrf), `Uploaded ${file.name}.`);
              }}
            />
          </label>
        )}
        {canManage && own && draft && (
          <>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() =>
                void run(() => evidenceAction(item.id, "submit", csrf), "Submitted for review.")
              }
            >
              Submit for review
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() =>
                void run(async () => {
                  await deleteEvidenceItem(item.id, csrf);
                  return null;
                }, "Deleted the draft.")
              }
            >
              Delete draft
            </button>
          </>
        )}
        {canManage && item.kind === "reference" && checksEnabled && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              void run(() => evidenceAction(item.id, "check", csrf), "Checked the address.")
            }
          >
            Check address
          </button>
        )}
      </div>
      {canReview && item.state === "submitted" && (!own || development) && (
        <div className="mt-3">
          <label className="form-label" htmlFor={`note-${String(item.id)}`}>
            Review note
          </label>
          <textarea
            id={`note-${String(item.id)}`}
            className="form-control mb-2"
            rows={2}
            maxLength={2000}
            value={note}
            onChange={(event) => {
              setNote(event.target.value);
            }}
          />
          <div className="btn-list">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() =>
                void run(() => evidenceAction(item.id, "accept", csrf, note), "Accepted.")
              }
            >
              Accept
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={!note.trim()}
              onClick={() =>
                void run(() => evidenceAction(item.id, "reject", csrf, note), "Rejected.")
              }
            >
              Reject
            </button>
          </div>
        </div>
      )}
      {canReview && item.state === "submitted" && own && !development && (
        <Notice tone="info" title="Someone else reviews it">
          You added this item, so another reviewer accepts or rejects it.
        </Notice>
      )}
    </section>
  );
}

/** Evidence people add: files and links, with the controls they support, reviewed by a second
 * person, and how long each stays fresh. */
export function EvidenceItemsPage() {
  const { canManage } = usePermissions();
  const [state, setState] = useState<EvidenceState | "">("");
  const [reload, setReload] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const loaded = useLoad(`evidence-items:${state}#${String(reload)}`, (signal) =>
    listEvidenceItems({ state: state || null }, signal),
  );
  const open = useLoad(`evidence-item:${String(openId)}#${String(reload)}`, (signal) =>
    openId === null ? Promise.resolve(null) : getEvidenceItem(openId, signal),
  );

  function changed(text: string, item: EvidenceItem | null) {
    setStatus(text);
    setOpenId(item?.id ?? null);
    setReload((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Evidence items"
        subtitle="Files and links people add for controls, reviewed by someone else, with how long each stays fresh."
      />
      {status && (
        <p role="status" className="alert alert-success text-body">
          {status}
        </p>
      )}
      {canManage && (
        <AddForm
          onAdded={(item) => {
            changed(`Added ${item.title} as a draft.`, item);
          }}
        />
      )}
      {open.data && (
        <ItemDetail
          key={`${String(open.data.id)}:${open.data.state}:${open.data.sha256 ?? ""}:${open.data.checked_at ?? ""}`}
          item={open.data}
          checksEnabled={loaded.data?.checks_enabled ?? false}
          onChanged={changed}
        />
      )}
      <label className="form-label">
        State
        <select
          className="form-select mt-1"
          value={state}
          onChange={(event) => {
            setState(event.target.value as EvidenceState | "");
          }}
        >
          <option value="">All</option>
          {(Object.keys(stateLabels) as EvidenceState[]).map((value) => (
            <option key={value} value={value}>
              {stateLabels[value]}
            </option>
          ))}
        </select>
      </label>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : !loaded.data ? (
        <Loading label="Loading evidence items…" />
      ) : loaded.data.items.length === 0 ? (
        <EmptyState title="No evidence items yet">
          Add a file or a link for a control, then submit it for review.
        </EmptyState>
      ) : (
        <div className="table-responsive">
          <table className="table card-table" aria-label="Evidence items">
            <thead>
              <tr>
                <th>Title</th>
                <th>Kind</th>
                <th>State</th>
                <th>Freshness</th>
                <th>Controls</th>
              </tr>
            </thead>
            <tbody>
              {loaded.data.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <button
                      type="button"
                      className="btn btn-link p-0"
                      aria-label={`Open ${item.title}`}
                      onClick={() => {
                        setOpenId(item.id);
                      }}
                    >
                      {item.title}
                    </button>
                  </td>
                  <td>{item.kind === "file" ? "File" : "Link"}</td>
                  <td>
                    <Badge tone={stateTones[item.state]}>{stateLabels[item.state]}</Badge>
                  </td>
                  <td>
                    <Badge tone={freshnessTones[item.freshness]}>
                      {freshnessLabels[item.freshness]}
                    </Badge>
                  </td>
                  <td>{controlText(item)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
