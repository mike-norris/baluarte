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
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
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
    return <p className="text-sm text-slate-600">No groups are assigned to managers yet.</p>;
  }
  return (
    <div className="space-y-2">
      <Problems messages={problems} />
      <div className="overflow-x-auto rounded border border-slate-200">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Groups assigned to managers</caption>
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              {["Manager", "Group", "Assigned", ""].map((title) => (
                <th key={title || "actions"} scope="col" className="px-3 py-2 font-medium">
                  {title || <span className="sr-only">Actions</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {scopes.map((scope) => (
              <tr key={scope.id}>
                <th scope="row" className="px-3 py-2 font-medium">
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
                    className={secondaryClass}
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
      const user = managers.find((item) => String(item.user_id) === userId);
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
    <div className="space-y-3 rounded border border-slate-200 p-3">
      <h3 className="font-medium">Assign a group</h3>
      <Problems messages={problems} />
      {users.data && managers.length === 0 ? (
        <p className="text-sm text-slate-600">
          No one with the manager role has signed in yet. Map a group to the manager role under
          Identity provider, and ask the person to sign in once.
        </p>
      ) : (
        <>
          <label className="block text-sm font-medium">
            Manager
            <select
              value={userId}
              onChange={(event) => {
                setUserId(event.currentTarget.value);
              }}
              className={fieldClass}
            >
              <option value="">{users.data ? "Choose a manager" : "Loading users…"}</option>
              {managers.map((user) => (
                <option key={user.user_id} value={String(user.user_id)}>
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
            className={buttonClass}
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
      <h2 id="manager-scopes-title" className="text-lg font-semibold">
        Manager groups
      </h2>
      <p className="text-sm text-slate-600">
        A manager sees time per person only for projects under the groups assigned here, subgroups
        included. Every assignment and its end is recorded in the audit log.
      </p>
      {scopes.error ? (
        <ErrorMessage error={scopes.error} retry={scopes.retry} />
      ) : scopes.data ? (
        <ScopeList scopes={scopes.data.items} csrf={csrf} onEnded={changed} />
      ) : (
        <p role="status" className="text-sm text-slate-600">
          Loading manager groups…
        </p>
      )}
      <AssignForm csrf={csrf} onAssigned={changed} />
      <p role="status" className="text-sm text-emerald-800">
        {message ?? ""}
      </p>
    </section>
  );
}
