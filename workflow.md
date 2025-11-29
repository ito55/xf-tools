## Workflow

# without xf-tools
```mermaid
flowchart TD;
    1[(input.mid)] --> 2[Cubase] --> 3[/pdf/]

```

# with xf-tools
```mermaid
flowchart TD;
    1@{ shape: doc, label: "input.mid" } --> 2[Cubase] --> |export|10@{ shape: docs, label: "musicsheet.pdf"};
    1 --> 3;
    subgraph additional
        direction TB
        3[convert.py] --> 4@{ shape: doc, label: "converted.musicxml" };
        4 --> |import MusicXML|5[Cubase] --> 6@{ shape: doc, label: "converted.cpr" };
    end
    6 --> |import tracks from project|2[Cubase]
```
