#import "@preview/touying:0.6.1": *
#import themes.simple: *

#show: simple-theme.with(aspect-ratio: "16-9")

#centered-slide[
  #speaker-note("Welcome to WebTypst on the server. This deck proves the container works: touying packages download, the resolver renders, and click-to-source resolves.")
  #text(size: 30pt, weight: "bold")[WebTypst — Server Edition]
  #v(1em)
  #text(size: 20pt)[Your workspace is ready.]
]

#slide[
  = Getting started
  - Open or create a project from the dashboard.
  - Edit on the left, preview on the right.
  - Run `claude` in the terminal (log in once).
]
