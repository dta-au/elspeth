Colour-coded badge naming one of ELSPETH's six pipeline component types. Use wherever a Source/Transform/Gate/Sink/Aggregation/Coalesce is referenced — catalog cards, graph nodes, validation copy.

```jsx
<TypeBadge type="source" />
<TypeBadge type="transform">llm</TypeBadge>
<TypeBadge type="gate" />
```

The colour mapping is fixed and load-bearing (source=aqua-green, transform=amber, gate=purple, sink=orange-red, aggregation=cyan, coalesce=cyan-teal) — do not recolour. Pass `children` to label the badge with a plugin name instead of the type word.
