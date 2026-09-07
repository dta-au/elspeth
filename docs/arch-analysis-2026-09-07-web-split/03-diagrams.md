# Proposed ownership boundary

This diagram shows team responsibilities, not a requirement for separate processes or deployments. The compiler is proposed; current validation/execution remains the initial backend implementation.

```mermaid
flowchart LR
  subgraph WEB["Web team"]
    UX["Browser UX and frontend tests"]
  end
  subgraph BACKEND["Backend team"]
    API["Application API"]
    APP["Composer, sessions, identity and blobs"]
    COMP["Validation / proposed compiler"]
    ENG["Execution engine, plugins and Landscape audit"]
  end
  UX <-->|"Requests, responses and progress events"| API
```
