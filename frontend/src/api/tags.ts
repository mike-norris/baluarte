/**
 * Client for Baluarte's own tags: key/value pairs such as ``team=payments`` that group projects
 * across the GitLab hierarchy. Tags never change GitLab and are not evidence of compliance.
 */

import { getJson } from "./client";
import type { Person } from "./policies";
import { sendJson } from "./send";

export interface Tag {
  id: number;
  key: string;
  value: string;
  description: string | null;
  /** ``#RRGGBB``, or null for the default. */
  color: string | null;
  created_by: Person;
  created_at: string;
  /** Groups assigned the tag directly. */
  groups: number;
  /** Projects it covers, directly or through a group. */
  projects: number;
}

export type TagTargetType = "group" | "project";

export interface TagTarget {
  type: TagTargetType;
  gitlab_id: number;
}

export interface TagAssignment {
  id: number;
  target_type: TagTargetType;
  target_gitlab_id: number;
  target_path: string;
  assigned_by: Person;
  assigned_at: string;
}

export interface TagDetail extends Tag {
  assignments: TagAssignment[];
}

/** A tag on an inventory row: assigned to the project itself or inherited from a group. */
export interface ItemTag {
  key: string;
  value: string;
  via: "project" | "group";
  /** The group it is inherited from, or the project's own path. */
  from_path: string | null;
}

export interface NewTag {
  key: string;
  value: string;
  description?: string;
  color?: string;
}

/** Keys and values: lowercase letters, digits, dot, dash and underscore; up to 63 characters. */
export const TAG_PART = /^[a-z0-9][a-z0-9._-]{0,62}$/;
const COLOR = /^#[0-9a-fA-F]{6}$/;
export const MAX_TAG_DESCRIPTION = 500;

/** Sentences for the API's reasons for refusing a tag change. */
export const tagRefusalReasons: Record<string, string> = {
  tag_exists: "a tag with that key and value already exists",
  key_conflict:
    "a selected item already has another value of this key assigned directly",
  tag_unknown: "that tag no longer exists",
  target_unknown: "a selected project or group is not in the inventory",
};

export function listTags(signal?: AbortSignal): Promise<{ items: Tag[] }> {
  return getJson("/api/v1/tags", undefined, signal);
}

export function getTag(id: number, signal?: AbortSignal): Promise<TagDetail> {
  return getJson(`/api/v1/tags/${String(id)}`, undefined, signal);
}

async function required<T>(result: Promise<T | null>): Promise<T> {
  const value = await result;
  if (value === null) throw new Error("The API returned no content.");
  return value;
}

export function createTag(tag: NewTag, csrfToken: string): Promise<Tag> {
  return required(
    sendJson<Tag>("POST", "/api/v1/tags", tag, csrfToken, tagRefusalReasons),
  );
}

export function updateTag(
  id: number,
  change: { description?: string | null; color?: string | null },
  csrfToken: string,
): Promise<Tag> {
  return required(
    sendJson<Tag>(
      "PATCH",
      `/api/v1/tags/${String(id)}`,
      change,
      csrfToken,
      tagRefusalReasons,
    ),
  );
}

export function assignTag(
  id: number,
  targets: TagTarget[],
  csrfToken: string,
): Promise<TagDetail> {
  return required(
    sendJson<TagDetail>(
      "POST",
      `/api/v1/tags/${String(id)}/assign`,
      { targets },
      csrfToken,
      tagRefusalReasons,
    ),
  );
}

export function unassignTag(
  id: number,
  targets: TagTarget[],
  csrfToken: string,
): Promise<TagDetail> {
  return required(
    sendJson<TagDetail>(
      "POST",
      `/api/v1/tags/${String(id)}/unassign`,
      { targets },
      csrfToken,
      tagRefusalReasons,
    ),
  );
}

/** A tag as ``key=value``, how people read it. */
export function tagText(tag: { key: string; value: string }): string {
  return `${tag.key}=${tag.value}`;
}

/** A tag as the ``key:value`` the list filters take. */
export function tagParam(tag: { key: string; value: string }): string {
  return `${tag.key}:${tag.value}`;
}

/** ``key:value`` from a URL, or null when it is not a valid tag. */
export function parseTagParam(
  value: string | null,
): { key: string; value: string } | null {
  const [key = "", rest = "", ...extra] = (value ?? "").trim().split(":");
  if (extra.length > 0 || !TAG_PART.test(key) || !TAG_PART.test(rest))
    return null;
  return { key, value: rest };
}

/** The problems with a new tag, by field; empty when it can be sent. */
export function tagProblems(
  tag: NewTag,
): Partial<Record<keyof NewTag, string>> {
  const problems: Partial<Record<keyof NewTag, string>> = {};
  const rule =
    "Use lowercase letters, digits, dots, dashes or underscores, up to 63 characters.";
  if (!TAG_PART.test(tag.key)) problems.key = rule;
  if (!TAG_PART.test(tag.value)) problems.value = rule;
  if ((tag.description ?? "").length > MAX_TAG_DESCRIPTION) {
    problems.description = `Keep it to at most ${String(MAX_TAG_DESCRIPTION)} characters.`;
  }
  if (tag.color && !COLOR.test(tag.color))
    problems.color = "Use a colour such as #1f6feb.";
  return problems;
}

/** How a tag reaches an item, in words. */
export function tagVia(tag: ItemTag): string {
  return tag.via === "group"
    ? `from group ${tag.from_path ?? "(unknown)"}`
    : "on the project";
}
