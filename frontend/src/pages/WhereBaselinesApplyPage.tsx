import { useEffect, useRef, useState } from "react";

import { LookupPanel } from "../policies/EffectivePanel";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { InheritanceTree } from "../policies/InheritanceTree";

export function WhereBaselinesApplyPage() {
  // Bumped after a change, so the tree and lookups reload.
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function changed(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <GovernanceHeader intro="Closer settings replace broader ones: a project's own setting, then its group, then that group's parents, then the whole instance. Each row shows what is set there and what it inherits." />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="text-sm font-medium">
          {message}
        </p>
      )}
      <InheritanceTree version={version} onChanged={changed} />
      <LookupPanel version={version} />
    </section>
  );
}
