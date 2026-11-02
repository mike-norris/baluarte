import { describe, expect, it } from "vitest";

import type { AssignmentTree, Target, TreeEffect, TreeNode } from "../api/policies";
import { buildTree, matches, type GroupNode } from "./inheritance";

const instance: Target = {
  target_type: "instance",
  target_id: 1,
  path: "https://gitlab.example.test",
  state: null,
};

function groupTarget(gitlabId: number, path: string, state = "active"): Target {
  return { target_type: "group", target_id: gitlabId, path, state };
}

function effect(
  name: string,
  version: number,
  status: TreeEffect["status"],
  assignedTo: Target,
  replaces: { version: number; assignedTo: Target }[] = [],
): TreeEffect {
  return {
    name,
    assignment_id: version * 100 + name.length,
    policy_id: version,
    version,
    yaml_digest: "a".repeat(64),
    status,
    assigned_to: assignedTo,
    replaces: replaces.map((item) => ({
      assignment_id: item.version,
      policy_id: item.version,
      name,
      version: item.version,
      yaml_digest: "b".repeat(64),
      assigned_to: item.assignedTo,
      outcome: "overridden",
    })),
  };
}

function node(
  gitlabId: number,
  path: string,
  parent: number | null,
  inEffect: TreeEffect[],
  extra: Partial<TreeNode> = {},
): TreeNode {
  return {
    target: groupTarget(gitlabId, path),
    parent_gitlab_id: parent,
    ancestry: "complete",
    gaps: [],
    assignments: [],
    in_effect: inEffect,
    ...extra,
  };
}

const platform = groupTarget(21, "acme/platform");

const data: AssignmentTree = {
  instance,
  instance_assignments: [
    {
      assignment_id: 4,
      policy_id: 4,
      name: "starter",
      version: 1,
      yaml_digest: "a".repeat(64),
    },
    {
      assignment_id: 3,
      policy_id: 3,
      name: "narrow",
      version: 1,
      yaml_digest: "a".repeat(64),
    },
  ],
  groups: [
    node(10, "corporate", null, [
      effect("starter", 1, "inherited", instance),
      effect("narrow", 1, "out_of_scope", instance),
    ]),
    node(20, "acme", null, [effect("starter", 1, "inherited", instance)]),
    node(21, "acme/platform", 20, [
      effect("starter", 2, "set_here", platform, [{ version: 1, assignedTo: instance }]),
    ]),
    node(22, "acme/platform/api", 21, [effect("starter", 2, "inherited", platform)]),
    node(40, "orphan/child", 99, [], {
      ancestry: "incomplete",
      gaps: [{ gitlab_id: 99, reason: "not_in_inventory" }],
    }),
  ],
  projects: [
    {
      target: {
        target_type: "project",
        target_id: 900,
        path: "corporate/website",
        state: "active",
      },
      parent_gitlab_id: 10,
      namespace_gitlab_id: 10,
      namespace_kind: "group",
      ancestry: "complete",
      gaps: [],
      assignments: [
        {
          assignment_id: 5,
          policy_id: 1,
          name: "starter",
          version: 1,
          yaml_digest: "a".repeat(64),
        },
      ],
      in_effect: [],
    },
  ],
};

function find(nodes: GroupNode[], path: string): GroupNode | undefined {
  for (const item of nodes) {
    if (item.group.full_path === path) return item;
    const below = find(item.children, path);
    if (below) return below;
  }
  return undefined;
}

describe("where baselines apply", () => {
  const tree = buildTree(data);

  it("nests groups under their parents and keeps orphans at the top", () => {
    expect(tree.instance.map((item) => item.name)).toEqual(["narrow", "starter"]);
    expect(tree.roots.map((item) => item.group.full_path)).toEqual([
      "acme",
      "corporate",
      "orphan/child",
    ]);
    expect(find(tree.roots, "acme/platform/api")).toBeDefined();
  });

  it("puts the API's resolution into words", () => {
    expect(find(tree.roots, "acme/platform")?.effects).toEqual([
      expect.objectContaining({
        name: "starter",
        version: 2,
        kind: "set_here",
        replaces: "v1 from This instance",
      }),
    ]);
    expect(find(tree.roots, "acme/platform/api")?.effects[0]).toMatchObject({
      kind: "inherited",
      from: "acme/platform",
    });
    expect(
      find(tree.roots, "corporate")?.effects.find((item) => item.name === "narrow")?.kind,
    ).toBe("out_of_scope");
  });

  it("says when the path above a group can't be confirmed", () => {
    expect(find(tree.roots, "orphan/child")?.unknown).toBe(
      "a parent group (GitLab ID 99) is not collected by Baluarte yet",
    );
    expect(find(tree.roots, "acme")?.unknown).toBeNull();
  });

  it("lists project assignments under their group", () => {
    expect(find(tree.roots, "corporate")?.projectAssignments).toEqual([
      {
        assignmentId: 5,
        projectGitlabId: 900,
        path: "corporate/website",
        name: "starter",
        version: 1,
      },
    ]);
  });

  it("searches paths, keeping the groups above a match", () => {
    const acme = find(tree.roots, "acme");
    expect(acme && matches(acme, "API")).toBe(true);
    expect(acme && matches(acme, "finance")).toBe(false);
    const corporate = find(tree.roots, "corporate");
    expect(corporate && matches(corporate, "website")).toBe(true);
  });
});
