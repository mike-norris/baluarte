import { useState } from "react";

import { listMfaEnrollments, resetMfaEnrollment, type MfaEnrollmentRow } from "../api/mfa";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { asApiError, cardClass, Problems } from "./parts";

function who(row: MfaEnrollmentRow): string {
  return row.display_name ?? row.email ?? `User ${String(row.user_id)}`;
}

/** Who has set up built-in TOTP, and resetting a lost authenticator (docs/SECURITY.md). */
export function MfaEnrollmentsCard({ csrf }: { csrf: string }) {
  const { session } = useAuth();
  const self = session.status === "signed_in" ? session.user.user_id : null;
  const [version, setVersion] = useState(0);
  const rows = useLoad(`mfa-enrollments#${String(version)}`, (signal) =>
    listMfaEnrollments(signal),
  );
  const [confirming, setConfirming] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [done, setDone] = useState<string | null>(null);

  async function reset(row: MfaEnrollmentRow) {
    setBusy(true);
    setProblems([]);
    try {
      await resetMfaEnrollment(row.user_id, csrf);
      setDone(
        `${who(row)}'s authenticator was reset and their sessions ended. They set up a new one at their next sign-in.`,
      );
      setConfirming(null);
      setVersion((value) => value + 1);
    } catch (error) {
      setProblems([asApiError(error).message]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="mfa-enrollments-heading" className={cardClass}>
      <h2 id="mfa-enrollments-heading" className="card-title">
        Authenticator apps
      </h2>
      <p className="text-secondary">
        People who set up an authenticator app for built-in TOTP. Reset one when its phone and
        recovery codes are lost: the authenticator and recovery codes are removed, the person&apos;s
        sessions end, and they set up a new one at their next sign-in. Resets are recorded in the
        audit log. You cannot reset your own.
      </p>
      <Problems messages={problems} />
      {done && (
        <p role="status" className="alert alert-success text-body">
          {done}
        </p>
      )}
      {rows.error && <ErrorMessage error={rows.error} retry={rows.retry} />}
      {rows.loading && <p className="text-secondary">Loading…</p>}
      {rows.data && rows.data.length === 0 && (
        <p className="text-secondary">No one has set up an authenticator app.</p>
      )}
      {rows.data && rows.data.length > 0 && (
        <div className="table-responsive">
          <table className="table table-vcenter">
            <thead>
              <tr>
                <th scope="col">Person</th>
                <th scope="col">Set up</th>
                <th scope="col">Recovery codes left</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.data.map((row) => (
                <tr key={row.user_id}>
                  <td>
                    {who(row)}
                    {row.email && row.display_name && (
                      <div className="small text-secondary">{row.email}</div>
                    )}
                  </td>
                  <td>{formatTime(row.confirmed_at)}</td>
                  <td>{row.recovery_codes_left}</td>
                  <td className="text-end">
                    {row.user_id === self ? null : confirming === row.user_id ? (
                      <span className="d-inline-flex gap-2">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void reset(row)}
                          className="btn btn-danger btn-sm"
                        >
                          {busy ? "Resetting…" : `Confirm reset for ${who(row)}`}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirming(null)}
                          className="btn btn-sm"
                        >
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => {
                          setDone(null);
                          setConfirming(row.user_id);
                        }}
                        className="btn btn-sm"
                        aria-label={`Reset ${who(row)}'s authenticator`}
                      >
                        Reset
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
