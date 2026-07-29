# Personal Access Token Dialog Layout Fix

**Status:** Approved in conversation on 2026-07-29

## Problem

The token creation form uses a four-column CSS Grid intended for Name, Access, Expires, and the
submit button. Its section title is also a direct grid child without an explicit placement, so
automatic placement consumes the first column with the title, shifts the three fields right,
and wraps the button onto the next row.

This is visible in production at the normal 780-pixel dialog width. The existing browser test
checks only the one-column mobile breakpoint and therefore does not detect the desktop
misplacement.

## Chosen Fix

Keep the existing JSX and responsive structure. On desktop:

- place the form's section title across all grid columns;
- keep Name, Access, Expires, and Create token together on the following row; and
- retain the existing field widths and button/input alignment.

At the existing narrow-screen breakpoint, the form continues to stack into one column without
horizontal overflow.

This is a CSS placement fix rather than a component restructure because the DOM already contains
the correct elements in the correct reading order.

## Regression Coverage

Extend the token-dialog browser test with a desktop-layout assertion that verifies:

- the section title occupies a row above the fields;
- Name, Access, Expires, and the submit button share the same form row;
- their left-to-right order is stable; and
- the dialog does not introduce horizontal page overflow.

Retain the existing mobile test for one-column stacking and the token creation/revocation
behavior test.

## Deployment

After unit, build, and browser verification:

1. commit and push the fix to `main`;
2. rebuild `tcb-workspace:latest` for future containers;
3. copy the new frontend production assets into the three existing stopped workspace
   containers without touching `/workspace`;
4. start each container briefly and verify the new asset and HTTP UI;
5. restore each container to its prior stopped state; and
6. verify the public site through Cloudflare.

No database, project file, token, or container mount changes are part of this fix.
