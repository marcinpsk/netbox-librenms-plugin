# LibreNMS Import Results UI

## 0. Review record

Round 1 did not ratify r1. It found five open interaction defects:

1. Extra source-name columns collapsed to 27 pixels and made rows unreadable.
2. Naming controls did not update names or their active-rule summary.
3. Role, rack, and VM controls did not expose their state transitions.
4. The persistent inspector kept placeholder controls for ready and existing objects.
5. Bulk controls contradicted the example row selection.

Revision r2 closed these findings in the mock. Optional source-name columns now receive bounded
width inside the responsive table container. Naming, role, rack, VM, inspector, and bulk-selection
controls update visible state. No claims were refuted.

Round 2 found one remaining hidden-state defect. A selected optional role on an existing VM row did
not leave a summary after its menu closed. Revision r3 adds the changed-options badge to that row.
The browser confirms that the badge remains visible after the menu closes.

Round 3 closed that defect and found no regression. The reviewer verdict is:

> RATIFY r3: import-results presentation mock and reviewed interactions, excluding production
> importer implementation.

## 1. Brief

### Decision

Choose an information hierarchy for the LibreNMS import result list. The current table gives every
source field and every conditional control a permanent column. The cluster, role, and rack controls
alone reserve 530 pixels before padding.

### Module and seam

The import-row presentation module owns identity, required setup, validation state, and actions. Its
interface is the validated row data that `DeviceImportTable` already receives. The HTMX row refresh
is an adapter at the existing seam. The LibreNMS request is not part of this UI decision.

The interface must remain deep: one row presentation must derive the visible name, applicable
controls, readiness, and actions from the same validation state. It must not duplicate import rules.

### Constraints

- A physical device requires a role. A rack is optional and depends on the matched site.
- The current importer treats a selected cluster as the switch to virtual-machine mode. It requires
  the cluster for a new virtual machine. A VM role is optional, and rack does not apply.
- Hostname and system name are LibreNMS source values. Import options choose the resulting NetBox
  name and can remove a domain suffix.
- Existing objects, conflicts, OOB pairs, and virtual chassis have distinct actions.
- Bulk selection and per-row actions must remain easy to find.
- The visual language must match NetBox and the dropdown selected as Sync Proposal A.
- Every proposal must use NetBox styles in both the light and dark schemes. The scheme control must
  remain available on the Sync and Import mock pages.

Changing the current VM placement rule is outside this mock scope. NetBox can represent more VM
placement shapes than this importer currently supports, but that needs a separate behavior design.

### Observable acceptance conditions

- The default result view has no horizontal scroll at a normal desktop width.
- The intended NetBox name is the primary identity.
- Column preferences cannot hide selection, required setup, or actions.
- Optional and type-specific details use progressive disclosure.
- A non-default hidden choice leaves a visible summary or count.
- Device, VM, stack, ready, blocked, warning, and existing-object rows remain distinguishable.
- The theme control changes the applied NetBox scheme and states the scheme it will switch to.

The mechanical guard for a production implementation is a browser check at supported breakpoints,
plus a table-interface test that excludes locked workflow fields from user column preferences.

## 2. Blind designs

The primary designer drafted three shapes from the brief before reading the other designs:

1. A focused table with fixed workflow columns, configurable source columns, and attached row
   options.
2. A narrow summary table with expandable configuration rows.
3. A state queue with a persistent inspector.

Three isolated designers then received the same evidence pointers and constraints. Each ran
`gpt-6-astra` with high reasoning effort and did not see the other designs.

- The minimal-interface designer recommended four fixed columns: selection, computed NetBox name,
  import configuration, and action. Source data moves behind disclosure.
- The flexibility designer recommended a fixed workflow core with configurable source context. It
  locked selection, name, required setup, and actions outside preferences.
- The common-caller designer recommended a compact table with the device role inline. Rack, VM,
  source comparison, and virtual-chassis detail move into an attached disclosure.

## 3. Divergence table

| Decision | Primary design | Blind designs | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Default shape | Focused table | All three recommend a focused table | Role is the repeated manual action. Cluster and rack are conditional. | Use focused table as Proposal A. | The default stays dense and familiar. |
| Source fields | Configurable columns | Minimal prefers disclosure. Flexible prefers configurable context. Common prefers disclosure. | Operators may need cross-row comparison, but hostname and system name are redundant by default. | Make Location and Hardware default context columns. Let users add Hostname and System name. | Comparison remains available without dominating the default. |
| Required role | Inline | All three keep it inline | Every new physical device needs a role. | Keep it in a locked Import setup column. | The common workflow needs no row expansion. |
| VM switch | Attached row option | All designs warn that cluster is the current mode switch. | A separate unsaved kind can drift from validation state. | Present VM conversion as an attached action. Show Cluster inline after conversion. | The mock does not invent a second mode source of truth. |
| Optional rack | Attached row option | All three hide it by default | Rack is optional and only valid after site resolution. | Hide it behind the row option and show a badge when set. | No permanent rack column. |
| Virtual chassis | Badge near identity | All designs move it out of a column | Most rows show only a dash. | Show it only on applicable names. | Stack information stays visible without an empty column. |
| Alternative review flow | Expandable table and inspector | Designers retain both as useful for exceptional cases | Conflicts need more space than routine imports. | Keep both as Proposals B and C. | The operator can compare density against review depth. |

## 4. Merged design r3

Proposal A is the recommended shape. Its fixed interface is Selection, NetBox object, Import setup,
and Actions. Location and Hardware are default source columns. Hostname and System name are optional
columns. The computed NetBox name remains in the fixed identity cell and states its source.

The Import options dropdown matches Sync Proposal A. Its badge counts settings that differ from the
default. A visible summary states the active naming rule. A per-row attached options control owns
rack and VM conversion. Optional values leave a badge when the control closes.

Proposal B tests a summary table with inline expansion. Proposal C tests a state queue with a
persistent inspector. They remain separate because they change the primary interaction and cannot
be reduced to styling differences.

When users add both source-name columns, the table keeps each at a readable width and scrolls inside
the table container. The page itself does not overflow. This is an explicit user choice and does not
change the compact default.

## 5. Validation and next action

Browser validation covers all three proposals at 1024 and 1280 pixels, both NetBox schemes,
column visibility, URL variant switching, row expansion, bulk selection, naming rules, Device or VM
field changes, and the inspector states. The design is ratified for presentation. The next action is
for the operator to compare the proposals and select one. If Proposal A wins, the first production
increment is the fixed identity, setup, and action columns with configurable source columns. It is
complete when the default production table fits at 1280 pixels, keeps the computed name primary,
and prevents preferences from hiding required workflow controls. Production implementation remains
out of the current scope.
