# Project Card Opening Feedback Design

**Date:** 2026-07-25

## Goal

Give immediate, card-local feedback after a user opens any project so a
roughly one-second PDF activation never looks like a missed click.

## Interaction

The Projects page tracks the ID of the project whose open request is in
flight. The selected card immediately displays a translucent overlay containing
an animated spinner and `Opening…`. The card exposes `aria-busy="true"` while
the request is active.

Only the selected card displays the overlay. While any open request is active,
all project-card open and menu actions are disabled to prevent competing
navigation, rename, duplicate, or delete operations. Creation state remains
independent.

The overlay is cleared when opening fails, after which the existing error toast
remains the failure feedback. A successful open navigates away from the
Projects page, so no extra completion animation is needed.

## Visual treatment

The card becomes the positioning boundary for a full-card overlay. The overlay
uses the existing panel and accent colors, a compact CSS border spinner, and a
short `Opening…` label. It does not reflow the card content.

The spinner animation is disabled under `prefers-reduced-motion: reduce`; the
label and overlay still communicate the state.

## Scope

The behavior applies to both Typst and PDF projects because both use the same
open request. No backend, routing, project data, or workspace behavior changes.

## Verification

A Puppeteer test will pause the real open request after a card click, assert
that the selected card is busy and shows the overlay while other project
actions are disabled, then release the request and verify navigation completes.
The existing project-routing and PDF workspace browser tests remain green.
