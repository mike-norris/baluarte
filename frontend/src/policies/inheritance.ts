/**
 * The "Where baselines apply" tree, built from the API's assignment tree
 * (``GET /api/v1/policy-assignments/tree``). The API resolves every group the same way as its
 * effective-policies read (docs/POLICY_ENGINE.md); this module only nests the groups and puts the
 * result into words.
 */

import type { AssignmentTree, Target, TreeAssignment, TreeEffect } from "../api/policies";
import { gapReasons } from "./format";

export type EffectKind = TreeEffect["status"];

export interface Effect {
  assignmentId: number;
  name: string;
  version: number;
  kind: EffectKind;
  /** Where the deciding assignment is: "This instance" or a group path. */
  from: string;
  /** The nearest farther assignment of the same baseline it replaces, as "v1 from corporate". */
  replaces: string | null;
}

export interface TreeGroup {
  gitlab_id: number;
  full_path: string;
  state: string;
  parent_gitlab_id: number | null;
}

/** A baseline applied directly to a project, listed under the project's group. */
export interface ProjectAssignment {
  assignmentId: number;
  projectGitlabId: number;
  path: string;
  name: string;
  version: number;
}

export interface GroupNode {
  group: TreeGroup;
  children: GroupNode[];
  effects: Effect[];
  /** Assignments made directly to projects in this group. */
  projectAssignments: ProjectAssignment[];
  /** Why the path above this group can't be fully confirmed, if it can't. */
  unknown: string | null;
}

export interface InstanceAssignment {
  assignmentId: number;
  name: string;
  version: number;
}

export interface Tree {
  instance: InstanceAssignment[];
  roots: GroupNode[];
}

function placeLabel(target: Target): string {
  return target.target_type === "instance" ? "This instance" : target.path;
}

function effect(item: TreeEffect): Effect {
  const next = item.replaces[0];
  return {
    assignmentId: item.assignment_id,
    name: item.name,
    version: item.version,
    kind: item.status,
    from: placeLabel(item.assigned_to),
    replaces: next ? `v${String(next.version)} from ${placeLabel(next.assigned_to)}` : null,
  };
}

/** Why the path above a target can't be confirmed, from the API's gaps, or null. */
export function gapText(gaps: { gitlab_id: number; reason: string }[]): string | null {
  const first = gaps[0];
  if (!first) return null;
  const why = gapReasons[first.reason] ?? first.reason;
  return `a parent group (GitLab ID ${String(first.gitlab_id)}) is ${why}`;
}

function instanceAssignment(item: TreeAssignment): InstanceAssignment {
  return {
    assignmentId: item.assignment_id,
    name: item.name,
    version: item.version,
  };
}

/** Nest the API's groups by ``parent_gitlab_id``, sorted by path at every level. */
export function buildTree(data: AssignmentTree): Tree {
  const onProjects = new Map<number, ProjectAssignment[]>();
  for (const project of data.projects) {
    if (project.parent_gitlab_id === null) continue;
    const list = onProjects.get(project.parent_gitlab_id) ?? [];
    for (const item of project.assignments) {
      list.push({
        assignmentId: item.assignment_id,
        projectGitlabId: project.target.target_id,
        path: project.target.path,
        name: item.name,
        version: item.version,
      });
    }
    onProjects.set(project.parent_gitlab_id, list);
  }

  const nodes = new Map<number, GroupNode>();
  for (const item of data.groups) {
    const id = item.target.target_id;
    nodes.set(id, {
      group: {
        gitlab_id: id,
        full_path: item.target.path,
        state: item.target.state ?? "active",
        parent_gitlab_id: item.parent_gitlab_id,
      },
      children: [],
      effects: item.in_effect.map(effect).sort((a, b) => a.name.localeCompare(b.name)),
      projectAssignments: onProjects.get(id) ?? [],
      unknown:
        item.ancestry === "incomplete"
          ? (gapText(item.gaps) ?? "its parent groups can't be followed")
          : null,
    });
  }
  const roots: GroupNode[] = [];
  for (const node of nodes.values()) {
    const parentId = node.group.parent_gitlab_id;
    const parent = parentId === null ? undefined : nodes.get(parentId);
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  const byPath = (a: GroupNode, b: GroupNode) => a.group.full_path.localeCompare(b.group.full_path);
  for (const node of nodes.values()) node.children.sort(byPath);
  roots.sort(byPath);
  const instance = data.instance_assignments
    .map(instanceAssignment)
    .sort((a, b) => a.name.localeCompare(b.name));
  return { instance, roots };
}

/** Whether ``node`` or anything below it matches ``search`` (a case-insensitive substring). */
export function matches(node: GroupNode, search: string): boolean {
  const text = search.trim().toLowerCase();
  if (!text) return true;
  return (
    node.group.full_path.toLowerCase().includes(text) ||
    node.projectAssignments.some((item) => item.path.toLowerCase().includes(text)) ||
    node.children.some((child) => matches(child, text))
  );
}
