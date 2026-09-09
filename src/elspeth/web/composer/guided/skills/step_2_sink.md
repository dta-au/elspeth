### This stage: the output

This stage decides **where the results go** — the pipeline's output (its
"sink"). The user describes it in plain language: "save it as a JSON file",
"one JSON row per page", "write a CSV".

To build it:

1. Choose the sink. When the prompt includes a list of policy-visible sink
   plugins, select from that list — it is what is available for this request.
   Call `list_sinks` when the prompt includes no such list, or when the list
   you have describes no sink that fits.
2. Settle its options. Configure the sink from whatever option detail the
   list gives you, and treat whatever it does not fully describe as a schema
   question: call `get_plugin_schema` on the sink you picked when an option
   is typed as an object or an array, when you need an option's allowed
   values, or when you would otherwise lean on a declared default instead of
   setting the option yourself.
3. Call `resolve_sink` with the output you've built — the sink plugin, its
   options, and a one-line note to the user about what you set up.

Configure the selected sink only from policy-visible live catalog data — the
sink list, its schema, and its assistance. Do not infer file formats, path
rules, write modes, collision behaviour, or output-schema options from a
plugin name or from examples that are not attached to this request. Preserve
any user constraint that the live schema can express; report an actual
capability gap when it cannot.

Some requirements are policy-level and do not appear in the option schema
itself: `composer_hints`. Both descriptions of a sink carry them — every
`list_sinks` entry alongside its `config_fields`, and `get_plugin_schema`
alongside the schema, which stays authoritative for enum values and nested
option shapes. Read them wherever you meet the sink, and treat them as
binding. When a hint says an option is required, must be set deliberately,
or is rejected when left implicit, set that option explicitly in your
resolution rather than relying on defaults. Always set the output path
option explicitly as well.

Pick the sink that matches what the user asked for and configure it yourself from
what they told you. Don't make them choose from a list, and don't ask them to
fill in options you can infer.
