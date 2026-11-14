import { useState } from "react";

import {
  assignScopes,
  endScope,
  listScopes,
  listUsers,
  personName,
  userName,
  type ManagerScope,
} from "../api/manager";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { formatTime } from "../time";
import { asApiError, cardClass, Problems } from "./parts";

function ScopeList({
  scopes,
  csrf,
  onEnded,
}: {
  scopes: ManagerScope[];
  csrf: string;
  onEnded: (message: string) => void;
}) {
  const [problems, setProblems] = useState<string[]>([]);
  const [ending, setEnding] = useState<number | null>(null);

  async function end(scope: ManagerScope) {
    setEnding(scope.id);
    setProblems([]);
    try {
      await endScope(scope.id, csrf);
      onEnded(`${personName(scope.user)} no longer has ${scope.group.path}.`);
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setEnding(null);
    }
  }

  if (scopes.length === 0) {
    return <p className="text-secondary">No groups are assigned to managers yet.</p>;
  }
  return (
    <div >
      <Problems messages={problems} />
      <div className="overflow-x-auto rounded border">
        <table className="table table-vcenter card-table mb-0">
          <caption className="visually-hidden">Groups assigned to managers</caption>
          <thead >
            <tr>
              {["Manager", "Group", "Assigned", ""].map((title) => (
                <th key={title || "actions"} scope="col" className="px-3 py-2 fw-medium">
                  {title || <span className="visually-hidden">Actions</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody >
            {scopes.map((scope) => (
              <tr key={scope.id}>
                <th scope="row" className="px-3 py-2 fw-medium">
                  {personName(scope.user)}
                </th>
                <td className="px-3 py-2">{scope.group.path}</td>
                <td className="px-3 py-2">
                  {`${formatTime(scope.assigned_at)} by ${personName(scope.assigned_by)}`}
                </td>
                <td className="px-3 py-2">
                  <button
                    type="button"
                    disabled={ending === scope.id}
                    onClick={() => void end(scope)}
                    aria-label={`End ${scope.group.path} for ${personName(scope.user)}`}
                    className="btn"
                  >
                    End
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AssignForm({ csrf, onAssigned }: { csrf: string; onAssigned: (message: string) => void }) {
  const users = useLoad("manager-users", (signal) => listUsers(signal));
  const [userId, setUserId] = useState("");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const managers = (users.data?.items ?? []).filter((user) => user.roles.includes("manager"));

  async function assign() {
    const found: string[] = [];
    if (!userId) found.push("Choose a manager.");
    if (!picked) found.push("Choose a group.");
    setProblems(found);
    if (found.length > 0 || !picked) return;
    setSaving(true);
    try {
      await assignScopes(Number(userId), [picked.gitlabId], csrf);
      const user = managers.find((item) => String(item.id) === userId);
      setPicked(null);
      onAssigned(`${user ? userName(user) : "The manager"} now has ${picked.path}.`);
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setSaving(false);
    }
  }

  if (users.error) return <ErrorMessage error={users.error} retry={users.retry} />;
  return (
    <div className="rounded border p-3">
      <h3 className="fw-medium">Assign a group</h3>
      <Problems messages={problems} />
      {users.data && managers.length === 0 ? (
        <p className="text-secondary">
          No one with the manager role has signed in yet. Map a group to the manager role under
          Identity provider, and ask the person to sign in once.
        </p>
      ) : (
        <>
          <label className="d-block fw-medium">
            Manager
            <select
              value={userId}
              onChange={(event) => {
                setUserId(event.currentTarget.value);
              }}
              className="form-control mt-1"
            >
              <option value="">{users.data ? "Choose a manager" : "Loading users…"}</option>
              {managers.map((user) => (
                <option key={user.id} value={String(user.id)}>
                  {userName(user)}
                </option>
              ))}
            </select>
          </label>
          <TargetPicker type="group" value={picked} onChange={setPicked} />
          <button
            type="button"
            disabled={saving}
            onClick={() => void assign()}
            className="btn btn-primary"
          >
            {saving ? "Assigning…" : "Assign group"}
          </button>
        </>
      )}
    </div>
  );
}

/** Which groups each manager follows; time per person is shown only within them. */
export function ManagerScopesCard({ csrf }: { csrf: string }) {
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const scopes = useLoad(`manager-scopes#${String(version)}`, (signal) =>
    listScopes("active", signal),
  );

  function changed(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  return (
    <section aria-labelledby="manager-scopes-title" className={cardClass}>
      <h2 id="manager-scopes-title" className="card-title">
        Manager groups
      </h2>
      <p className="text-secondary">
        A manager sees time per person only for projects under the groups assigned here, subgroups
        included. Every assignment and its end is recorded in the audit log.
      </p>
      {scopes.error ? (
        <ErrorMessage error={scopes.error} retry={scopes.retry} />
      ) : scopes.data ? (
        <ScopeList scopes={scopes.data.items} csrf={csrf} onEnded={changed} />
      ) : (
        <p role="status" className="text-secondary">
          Loading manager groups…
        </p>
      )}
      <AssignForm csrf={csrf} onAssigned={changed} />
      <p role="status" className="text-success-emphasis">
        {message ?? ""}
      </p>
    </section>
  );
}
