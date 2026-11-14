import { useState, type ReactNode } from "react";
import { Link } from "react-router";

import { getProfile, PROFILE, type Profile, type Violation } from "../api/compliance";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { cardClass, Values } from "../settings/parts";
import { formatTime } from "../time";
import { ComplianceForm } from "./ComplianceForm";
import { ExceptionRequestForm, requestedMessage } from "./ExceptionRequestForm";
import { COMPLIANCE, frameworkList, settingInfo, settingOrder } from "./format";

function settingValue(profile: Profile, name: (typeof settingOrder)[number]): ReactNode {
  const value = profile.settings[name];
  const info = settingInfo[name];
  const text =
    typeof value === "boolean" ? (value ? "On" : "Off") : `${String(value)} ${info.unit ?? ""}`;
  return profile.sources[name] === "deployment" ? `${text} (set by the deployment)` : text;
}

/** Settings → Compliance: the operating profile, edited by administrators. */
export function ComplianceCard({ csrf }: { csrf: string }) {
  const { refresh } = useAuth();
  const [version, setVersion] = useState(0);
  const loaded = useLoad(`settings-compliance#${String(version)}`, (signal) =>
    getProfile(PROFILE, signal),
  );
  const [editing, setEditing] = useState(false);
  const [requesting, setRequesting] = useState<Violation | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  let body: ReactNode;
  if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (loaded.loading || loaded.data === undefined) {
    body = (
      <p role="status" className="text-secondary">
        Loading the compliance profile…
      </p>
    );
  } else if (editing) {
    body = (
      <>
        <ComplianceForm
          path={PROFILE}
          csrf={csrf}
          profile={loaded.data}
          enforceMinimums={false}
          onSaved={() => {
            setEditing(false);
            setRequesting(null);
            setMessage("Compliance profile saved. The Compliance page shows the new posture.");
            setVersion((value) => value + 1);
          }}
          onError={(error) => {
            if (error.status === 401) void refresh();
          }}
          onCancel={() => {
            setEditing(false);
            setRequesting(null);
          }}
          violationAction={(violation) =>
            requesting?.control_id === violation.control_id ? null : (
              <button
                type="button"
                onClick={() => {
                  setRequesting(violation);
                }}
                className="btn"
              >
                Request an exception
                <span className="visually-hidden"> for {settingInfo[violation.setting].label}</span>
              </button>
            )
          }
        />
        {requesting && (
          <ExceptionRequestForm
            controls={[
              {
                id: requesting.control_id,
                title: settingInfo[requesting.setting].label,
              },
            ]}
            controlId={requesting.control_id}
            csrf={csrf}
            onRequested={(exception) => {
              setRequesting(null);
              setMessage(
                `${requestedMessage(exception)}${exception.in_force ? " Save the profile again." : ""}`,
              );
            }}
            onCancel={() => {
              setRequesting(null);
            }}
            onError={(error) => {
              if (error.status === 401) void refresh();
            }}
          />
        )}
      </>
    );
  } else {
    const profile = loaded.data;
    body = (
      <>
        {profile.configured ? (
          <Values
            rows={[
              ["Frameworks", frameworkList(profile.frameworks)],
              ...settingOrder.map((name): [string, ReactNode] => [
                settingInfo[name].label,
                settingValue(profile, name),
              ]),
              [
                "Last changed",
                profile.updated_at
                  ? `${formatTime(profile.updated_at)}${profile.updated_by ? ` by ${profile.updated_by}` : ""}`
                  : "Never",
              ],
            ]}
          />
        ) : (
          <p role="alert" className="text-warning-emphasis">
            No compliance profile has been chosen. Choose the frameworks Baluarte must meet so its
            retention, sessions and change review follow them.
          </p>
        )}
        <div className="d-flex flex-wrap align-items-center gap-3">
          <button
            type="button"
            onClick={() => {
              setMessage(null);
              setEditing(true);
            }}
            className="btn btn-primary"
          >
            {profile.configured ? "Edit profile" : "Choose a profile"}
          </button>
          <Link to={COMPLIANCE} className={`text-sm ${linkClass}`}>
            See the compliance posture
          </Link>
        </div>
      </>
    );
  }

  return (
    <section id="compliance" aria-labelledby="compliance-heading" className={cardClass}>
      <h2 id="compliance-heading" className="card-title">
        Compliance profile
      </h2>
      <p className="text-secondary">
        The frameworks Baluarte itself must meet, and the retention, session and review settings
        they require. Every change is audited and appears in evidence exports.
      </p>
      <p role="status" className="text-success-emphasis">
        {message ?? ""}
      </p>
      {body}
    </section>
  );
}
