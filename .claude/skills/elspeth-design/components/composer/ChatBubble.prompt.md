A composer chat bubble. `role` picks the treatment: user (right, green tint), assistant (left, white tint + 2px left accent — reads as "system attestation"), system (centred, italic, muted notice).

```jsx
<ChatBubble role="user">Classify these tender submissions for abuse.</ChatBubble>
<ChatBubble role="assistant">I added a CSV source and an llm transform. Validate when ready.</ChatBubble>
<ChatBubble role="system">Pipeline version 3 — graph validated.</ChatBubble>
```

Measure is capped at ~68ch. Compose tool-call cards and ribbons inside the assistant bubble's children.
