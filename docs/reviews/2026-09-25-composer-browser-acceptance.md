# Composer browser acceptance after the epoch-68 refresh

On 2026-09-25, the local Caddy/systemd installation at
`https://elspeth.foundryside.dev` was rebuilt and restarted from
`release/0.8.1` at `e991b35fb1bb8b7e78a609800f48e2f01416afe2`. The
service process started at 09:52:44 AEST, after that commit. The frontend
bundle was rebuilt from the locked npm dependencies. The HTML and its emitted
JS/CSS entry assets matched the local build byte-for-byte over both the Unix
socket and public HTTPS route. Direct and public `/api/ready` returned ready,
with current session and Landscape schemas; the unit had zero restarts after
the battery.

The prior session store was at epoch 67. With operator approval, its database
and sidecars were archived under
`data/archives/local-refresh-epoch67-20260924T234903Z/` before epoch 68 was
created. The credential store `data/auth.db` was preserved, and the existing
`dta_user` local account was readmitted as an active administrator. The old two
sessions remain in the archive, outside the live UI. Six optional transforms
used by the battery were added to the operator-local plugin allowlist:
`batch_top_k`, `json_explode`, `keyword_filter`, `truncate`, `type_coerce`, and
`value_transform`. The browser catalog then showed 12 transforms.

Each fixture in `tests/fixtures/composer_convergence/` was driven through a
fresh authenticated browser session: upload its input file, send its natural
language turn(s), inspect validation and any prompt card, confirm Run in the
UI, and inspect the result. No pipeline graph was submitted by the harness.

| Case | Browser session | Run result | Output observation |
| --- | --- | --- | --- |
| 01 cleanup/edit | [session](https://elspeth.foundryside.dev/#/cc64235c-bf63-4bf4-884e-8b6cd6c413a3) | 3 input, 3 succeeded | `id,name,note`; notes `abc`, `xy`, `123` after the edit |
| 02 numeric/quarantine | [session](https://elspeth.foundryside.dev/#/d296d62a-93c1-4872-9625-5d5e3aef6211) | 5 input, 3 succeeded, 2 intentionally failed into quarantine | `large.csv`: `o1`, `o4`; `small.csv`: `o3`; final quarantine: unchanged `o2`, `o5` |
| 03 keyword route/edit | [session](https://elspeth.foundryside.dev/#/990f4aac-24df-442a-9cef-65b8a342a42a) | 5 input, 3 succeeded, 2 intentionally failed into quarantine | Both whole-word patterns in the spec; priority `t1,t5`, standard `t4`, final quarantine `t2,t3` |
| 04 reference default/edit | [session](https://elspeth.foundryside.dev/#/f9093aac-384a-4da0-bd3d-4a7343c8598b) | 3 input, 3 succeeded | Four ordered CSV columns; prices `10`, `7`, `-1` |
| 05 complaint/SLA | [session](https://elspeth.foundryside.dev/#/e6751717-d1cb-4eb3-82de-7ee975a86541) | 6 input, 6 succeeded | Two each of `billing/24`, `outage/4`, `other/48`; original IDs and complaints retained |
| 06 structured extraction | [session](https://elspeth.foundryside.dev/#/7e53b2d0-31b7-41e4-b24e-61fbb7f3856e) | 3 input, 3 succeeded | `B04,1,false` routed normal; `A17,3,true` and `C99,12,true` routed urgent |
| 07 grouped frequency | [session](https://elspeth.foundryside.dev/#/731b9cdf-356a-44f2-8d30-591acfa3d844) | 6 input, 2 summary tokens succeeded | East: red 2, blue 1; west: green 2, red 1; numeric counts and rates |
| 08 JSON expansion | [session](https://elspeth.foundryside.dev/#/8a0c5691-7795-46ea-860d-849ccf27225d) | 2 input, 3 expanded tokens succeeded | `j1/red/0`, `j1/blue/1`, `j2/green/0` |
| 09 fork/coalesce | [session](https://elspeth.foundryside.dev/#/b6a52e8b-5cd9-4d7c-a4d6-ec4aa04d23cd) | 3 input, 9 branch/join tokens succeeded | `(x,doubled,squared)`: `(2,4,4)`, `(5,10,25)`, `(8,16,64)` |
| 10 multiline text | [session](https://elspeth.foundryside.dev/#/86431528-8174-425a-81ed-d96c9641c1fd) | 3 input, 6 line tokens succeeded | Browser-downloaded `lines.txt` exactly `alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\n` |

Every run showed complete audit closure in the UI. The session and Landscape
databases were captured through SQLite's read-only backup API after the runs;
both snapshots passed `integrity_check`. The ten session run records match the
table above. Selecting the final artifact for each sink publication chain and
applying the fixture output oracle passed **10/10** cases. A negative control
that changed a value in case 01 was rejected. The two runs marked
`completed_with_failures` are the specified rejection-to-quarantine workflows;
their current quarantine files contain both rejected rows.

The Composer audit recorded 93 successful DeepSeek planner calls served by
Together and 29 successful GLM advisor calls. Across these browser sessions,
18 `preview_pipeline`, 10 `list_blobs`, 6 `list_composer_blobs`, and 2
`get_expression_grammar` calls were wire-conformant. Five authoring-tool
rejections occurred in cases 05, 09, and 10; the planner recovered and all
five are distinct from the zero-argument regression.

The full output manifest retains every cumulative sink publication. In cases
02 and 03 it shows an earlier `quarantine.csv` entry whose recorded hash no
longer matches the file after the second row was appended. Manually opening
that historical entry returns HTTP 409 and the browser reports a content-drift
error. The final entry is downloadable, contains both rows, matches its audit
hash, and passes the fixture oracle. This is a **remaining UI clarity issue**:
the historical entry is not labelled as an earlier publication, so an
operator can mistake the expected supersession for lost output. The final
browser console inventory contained one error: this deliberate 409.
