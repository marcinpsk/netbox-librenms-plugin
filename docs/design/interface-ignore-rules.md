# Interface ignore rules

Status: **RATIFIED (split core + cable mechanism), 2026-09-23; type-vs-links validation ratified
2026-09-24, see sections 6 and 7.** Target branch: `feat/interface-member-badges`.
Deferred: interface-row concurrency (AC5) and module port binding.

## 1. Brief

### Decision

Operators want to keep some LibreNMS ports out of interface sync on some platforms (for example
VLAN SVIs, loopbacks, or internal ports on one vendor). They also want to set a NetBox interface
type from the port name and the platform, not only from `ifType` and speed. A planned background
sync job must apply the same rules as the UI.

### Settled by the operator (not open)

1. The rules extend `InterfaceTypeMapping`. There is no second table and no saved search.
2. A rule gets an optional NetBox `Platform` foreign key. It is not a LibreNMS OS string.
3. A rule gets an optional name regex, matched against `ifName` or `ifDescr`.
4. A rule gets an action: **Set type** (today's behaviour) or **Ignore**.
5. Ignore always wins over Set type.
6. Ignore never deletes or changes an interface that exists in NetBox.
7. The interfaces sync table hides ignored ports by default. An "N ignored" toggle shows them
   greyed out, with the matching rule and no sync checkbox.

### Open decisions

- A. The specificity order between Set-type rules, and what happens at a tie.
- B. The uniqueness constraints once platform and pattern are part of a rule's key.
- C. Which consumers must honour Ignore: the interfaces-tab sync writer, the IP tab
  resolve-or-create, the cables tab far end, VC member rows, OOB rows, LAG members and parents
  whose partner is ignored, the future job.
- D. The shape of the one shared matcher (the module, its interface, where it is loaded once per
  request), and how the table's type-mapping cache changes.
- E. Which platform a row reads: the synced device, the VC member that owns the port, or a VM.

### Constraints

- One definition read by the table, the writer and a future job (repo rule: the row sync diff is
  one definition, `interface_diff.py`).
- Fail closed on ambiguity; no silent fallback.
- No backwards-compatibility layer. A migration is allowed. The model keeps working for existing
  `ifType` + speed rows without edits.
- Regexes are Python `re`, validated at save time.
- The PR must stay at or below 100 changed files (now 71).

### Observable acceptance conditions

1. With an enabled Ignore rule `name ^Vlan` for platform P, a device on P shows no `Vlan*` rows
   in the interfaces tab by default. The toggle shows them greyed out with the rule name and no
   checkbox. A device on another platform still shows them.
2. A sync POST that names an ignored port does not create or change a NetBox interface.
3. A Set-type rule `platform P, name ^Te` makes a `Te1/1` port on P sync as that rule's type,
   and the table's Type column shows the same type.
4. Existing `ifType` + speed rows give the same result as before for every port that no new rule
   matches.
5. Two equal-rank Set-type rules that both match one port show the row as ambiguous, and the
   writer does not set a type.

### Evidence pointers

- Model: `netbox_librenms_plugin/models.py` (`InterfaceTypeMapping`; compare `InventoryIgnoreRule`
  for an existing action/skip rule and `PortStackLagPattern` for per-OS regexes).
- Selection rule: `utils.select_interface_type_mapping`.
- Writer: `interface_sync.get_netbox_interface_type`, `update_interface_from_port`,
  `resolve_or_create_interface_from_port`.
- Table: `tables/interfaces.py` (`render_type`, `_interface_type_mapping_cache`).
- Tab view: `views/base/interfaces_view.py` (`get_context_data`, `get_table`).
- Sync POST: `views/sync/interfaces.py`.
- Other consumers: `views/sync/ip_addresses.py` (resolve-or-create), `views/sync/cables.py`
  (far-end type), `interface_diff.py`.
- Mapping UI and API: `forms.py`, `filters.py`, `tables/mappings.py`, `views/mapping_views.py`,
  `api/serializers.py`.

## 2. Designs

Blindness: design A (Claude Opus 5.5, main session) was written from the brief and the code alone.
Design B (codex `gpt-6-astra`, effort xhigh, `-s read-only`, fresh context) got only section 1 and
a fixed list of required sections. Neither saw the other before both were complete.

### 2A. Claude design

**Model.** On `InterfaceTypeMapping`:

- `action`: `CharField`, choices `set_type` / `ignore`, default `set_type`. The migration gives
  existing rows `set_type`.
- `platform`: `ForeignKey(dcim.Platform, null=True, blank=True, on_delete=CASCADE)`. Never
  `SET_NULL`: that would turn a platform-scoped Ignore into a global one. NetBox's delete
  confirmation lists the rules it removes.
- `name_pattern`: `CharField(max_length=200, blank=True, default="")`. Python `re`, compiled in
  `clean()`, matched with `re.search` against `ifName` and against `ifDescr`. Either match counts.
- `librenms_type` becomes `blank=True`; `""` means any type.
- `netbox_type` becomes `blank=True`, no default. `clean()`: Set type requires it; Ignore requires
  it blank.
- `librenms_speed` stays. `clean()` rejects a speed on an Ignore rule (a speed threshold has no
  meaning for a filter) and a speed without a type (today's semantics).
- `clean()` requires at least one of platform, name pattern, type.
- Uniqueness is over the match fields, not the action: one rule per
  `(platform, name_pattern, librenms_type, librenms_speed)`. With `""` for the text fields, only
  `platform` and `librenms_speed` can be NULL, so four conditional `UniqueConstraint`s (platform
  null/set x speed null/set) cover it on every PostgreSQL version NetBox supports.
  `nulls_distinct=False` needs PostgreSQL 15, so it is not used. `clean()` gives the readable
  error.

**Matching.** One rule set, loaded once (all rows, patterns compiled). For a port and a platform id:

1. Candidates: rules whose platform is NULL or equals the row's platform, whose type is `""` or
   equals `ifType`, whose pattern is `""` or matches `ifName` or `ifDescr`, and (Set type only)
   whose speed is NULL or at or below the port speed.
2. Any Ignore candidate: the port is ignored. The decision names every matching Ignore rule.
3. Otherwise rank Set-type candidates by the tuple (platform set, pattern set, type set, speed or
   -1). The top rank wins. The speed part keeps today's rule: the highest threshold at or below the
   port speed beats the any-speed row.
4. Two or more candidates on the top rank: ambiguous. The writer treats the port as unmapped (a
   new interface gets `other`, an existing type is kept). The Type cell names the rules.

Worked examples: global `ethernetCsmacd -> 1000base-t` and `P, ^Te -> 10gbase-x+`: a `Te1/1`
`ethernetCsmacd` port on P gets `10gbase-x+`; on platform Q it gets `1000base-t`. Global `^Te` and
`P, ethernetCsmacd`: on P the platform rule wins (platform outranks pattern). `P, ^Te` and
`P, /1$`: `Te1/1` on P is ambiguous.

Row platform: the `platform_id` of the NetBox object the row writes to: the resolved chassis
member for a VC row (`resolve_interface_row_device`), the device, or the VM. An OOB row reads the
device it writes to. A device with no platform matches only global rules.

**Shared matcher.** New module `interface_rules.py`:

- `InterfaceRules.load()` reads every rule once and compiles patterns.
- `rules.decide(port, platform_id) -> RuleDecision` with `ignored_by: tuple[rule, ...]`,
  `type_rule: rule | None`, `ambiguous: tuple[rule, ...]`, and the property `netbox_type`.
- It replaces `utils.select_interface_type_mapping`, `interface_sync.get_netbox_interface_type`
  and the table's `_interface_type_mapping_cache`. All three are deleted. Each request loads the
  rule set once and threads it to its consumers (the table, the sync POST, the IP tab, the cables
  far-end create).

**Consumers.**

| path | ignored port |
|---|---|
| interfaces tab render (`interfaces_view.get_context_data`) | Enrichment runs on every port, so an ignored port bound to an interface still counts as matched and that interface never shows as NetBox-only. Name decisions (`synced_interface_names`, `reported_name_owners`) keep ignored ports as input, so hiding a port never changes another row's name. The row gets `ignored_by`; the table data drops it unless the toggle is on. |
| sync POST loop (`sync_selected_interfaces`) | Skip with a reason naming the rule. A crafted POST cannot sync it. |
| `auto_select_lag_members` closure | Never adds an ignored port and does not walk through one. |
| relationship edges (`_sync_interface_relationships`) | An ignored source is not written. An edge from a synced source to an ignored target that exists in NetBox is written, because only the source interface changes. |
| single-row verify, Rebind, single relationship views | Refuse an ignored port with the same reason. |
| IP tab `resolve_or_create_interface_from_port` | Return an interface already bound to the port. Refuse to adopt by name or create. Adopting writes the port id, which changes the interface. |
| cables far-end create | Refuse and do not offer the action for an ignored far-end port (the far device's platform). |
| row sync diff (`compute_row_sync_state`) | Unchanged; its `netbox_type` input comes from the decision. |
| future job | Calls `decide()` per port. |

**Table UI.** The tab view counts ignored rows and passes the count to the template. A `show_ignored`
GET/POST flag (carried like `interfaces_page`) decides whether ignored rows are in the table data.
Pagination therefore counts only the rows it shows. Shown ignored rows are greyed, carry the rule
names, and have no checkbox, so select-all and off-page selection never include them.

**Mapping UI and API.** The form, filter set, filter form, list table, bulk import, YAML export
and REST serializer gain `action`, `platform` (by slug in import) and `name_pattern`. The list
page title becomes "Interface Mappings".

**Failure behaviour.** An invalid regex fails `clean()`. A deleted platform removes its rules.
Ambiguity is visible in the table and never writes a type.

**Tests.** Real DB, no mocks: model `full_clean()` cases; `decide()` against real rows for
every rank and the tie; interfaces tab GET with a real Ignore rule (row hidden, count shown,
toggle shows it without a checkbox); sync POST that names an ignored port (nothing created, skip
message); IP tab create refused; migration test that existing rows keep their result.

**Files.** About 14 new to the PR: models, migration, forms, filters, tables/mappings,
serializers, the matcher module, 1-2 templates, 3 test files, this record. The PR goes from 71 to
about 85.

### 2B. Codex design

The full text is not kept. Section 3 records every divergence between 2A and 2B and how each was
settled.

## 3. Divergence table

| # | decision | A (Claude) | B (codex) | evidence | disposition |
|---|---|---|---|---|---|
| 1 | extra fields | none | `name` label, `enabled` flag | not in the brief; `description` and `__str__` identify a rule | A. Reopen if the operator asks for a toggle. |
| 2 | platform on delete | `CASCADE` | `PROTECT` | `PlatformMapping.netbox_platform` is `CASCADE` (`models.py:806`); NetBox's delete confirmation lists dependants | A. Never `SET_NULL`. |
| 3 | Ignore output type | `""` | `NULL` | `netbox_type` has choices; NULL reads as "no output" without a sentinel | B. |
| 4 | unique key | match fields only (Ignore and Set with the same selectors are rejected) | includes `action` (both allowed; Ignore wins) | with Ignore always winning, a Set rule on the same selectors never takes effect | A. A rule that can never apply is rejected at save time. |
| 5 | speed | only with a type, never on Ignore | a selector on its own, allowed on Ignore | today speed only refines a type (`select_interface_type_mapping`) | A. Fewer meanings. |
| 6 | DB checks | `clean()` only | check constraints for action/type pairing and "at least one selector" | fail fast at the DB as well | B. |
| 7 | ambiguity | treated as unmapped, row syncs | row operation blocked | global rule: no silent fallbacks | B. Brief condition 5 tightened: no interface write for the row. |
| 8 | edges to an ignored endpoint | written when only the source changes | skipped if either end is ignored or ambiguous | `_prepare_bulk_lag_aggregate` promotes the aggregate (`views/sync/interfaces.py:736`), so the target changes too | B. |
| 9 | VC owner unresolved | read the platform from `resolve_interface_row_device` | block the row, no fallback | that helper returns the page device on failure by default (`utils.py:1133`) | B. Call with `return_device_on_failure=False`. |
| 10 | where the gate lives | in the view loops | inside `update_interface_from_port` and `resolve_or_create_interface_from_port`, which require `rules` | a caller cannot bypass a gate in the shared writer | B. |
| 11 | load once | threaded by callers | cached on the request (`interface_rules_for_request`), `load()` for a job | the sync POST renders its response via a second view (`views/sync/interfaces.py:321`); `_remember_interface_name_per_platform` already caches on the request (`utils.py:1942`) | B. |
| 12 | invalid regex that bypassed `clean()` | not handled | `load()` raises; the request shows a configuration error | fail fast | B. |
| 13 | row diff | unchanged | new `ignored` / `ambiguous` states | ignored and ambiguous rows render with no actions, so the diff must know | B. |
| 14 | stale off-page selections | server gate refuses them with a skip message | JS prunes them using eligibility metadata | the server gate already refuses every ignored port | A. Round 1 attacks this. |
| 15 | NetBox-only delete revalidation | not in scope | revalidate presence on delete | a stale selection deleting an interface is not specific to Ignore | A. Out of scope. |
| 16 | AST guard | none | a test that no module outside the matcher selects mappings at run time | it keeps the one-definition rule mechanical | B. |
| 17 | IP assignment to an existing interface | allowed (the IP is another object) | blocked | a product decision about what Ignore means | **Operator.** |
| 18 | cable sync between existing interfaces | allowed | blocked | as 17 | **Operator.** |
| 19 | module port binding (`_bind_interface_librenms_id`) | missed | gated; needs port selectors carried into module enrichment | the binder writes `librenms_id` and `module_id` (`views/sync/modules.py:734`) | Split candidate, **operator.** |
| 20 | files | about 14 new | 51 touched, 93 total | the difference is almost all rows 17 to 19 plus JS pruning | Follows 14 and 17 to 19. |

Agreed by both (not re-litigated): the lexicographic rank (platform, pattern, type, speed); ties
never broken by id or order; Ignore evaluated first; `re.search` on `ifName` or `ifDescr`, and a
missing name cannot match; four partial unique constraints, not `nulls_distinct`; the owner's
platform, never a VM's host; presence computed before rows are hidden; filtering before
pagination; no checkbox on ignored rows; delete `select_interface_type_mapping`,
`get_netbox_interface_type` and the table cache.

Operator dispositions (2026-09-23): rows 17 and 18 go to **B** (Ignore blocks IP assignment and
cable sync on the port). Row 19 (module port binding) is **deferred** to a follow-up. Row 1: no
`enabled` field.

## 4. Merged design and ratify rounds

### r1 (merged candidate)

**Meaning.** An Ignore rule makes a LibreNMS port inert for the plugin's port-driven writes. The
plugin does not create, update, bind, relink or promote the port's interface. It does not assign
an IP to that interface, and it does not create, tag or replace a cable on it. It deletes nothing.
Ambiguous Set rules block the same writes for that port. Module port binding is out of scope
(follow-up).

**Model** (`InterfaceTypeMapping`, migration `0022`, irreversible):

- `action` (`set_type` / `ignore`, default `set_type`); `platform` FK to `dcim.Platform`,
  `null=True`, `on_delete=CASCADE`; `name_pattern` (`""` = any); `librenms_type` now `blank`
  (`""` = any); `netbox_type` now `null=True` (NULL on Ignore); `librenms_speed` unchanged.
- `clean()`: strip `librenms_type`; keep `name_pattern` verbatim; compile it with
  `validate_regex_field()`; at least one of platform / type / pattern; Set type requires
  `netbox_type`, Ignore requires NULL; speed requires a type and is rejected on Ignore; readable
  uniqueness error.
- Unique over `(platform, librenms_type, name_pattern, librenms_speed)`, action excluded, as four
  partial `UniqueConstraint`s. Check constraints: action/type pairing, at least one selector.

**Matching** (`interface_rules.py`):

- `InterfaceRuleMatcher.load()`: one query, compiled patterns; raises `RuleConfigurationError`
  on a pattern that does not compile.
- `matcher.decide(port, *, platform_id) -> RuleDecision(kind, netbox_type, rules)`, `kind` in
  `UNMAPPED`, `SET_TYPE`, `IGNORE`, `AMBIGUOUS`. No DB access, no side effects.
- `interface_rules_for_request(request)` caches one matcher on the request. A job calls `load()`.
- Selectors are AND. The pattern is `re.search` on `ifName` or `ifDescr`, raw values; a
  non-`str` name cannot match. Type is exact equality with `ifType`. Speed is a minimum in Kbps via
  `convert_speed_to_kbps`.
- Any matching Ignore gives `IGNORE` (all matches listed). Otherwise the top Set rank by
  `(has_platform, has_pattern, has_type, has_speed, speed)` wins. Two at the top rank give
  `AMBIGUOUS`, even when their outputs agree.
- Platform: the owner of the interface. A standalone device, the VM itself, or the VC member from
  `resolve_interface_row_device(..., return_device_on_failure=False)`. An unresolved VC owner
  blocks the row. An OOB row reads its target device. A cable far end reads the remote device.

**Gate.** `update_interface_from_port` and `resolve_or_create_interface_from_port` take a required
`rules` argument and raise `PortSyncBlocked(decision)` before any write. The callers turn it into
their per-row skip or error result. Deleted: `utils.select_interface_type_mapping`,
`interface_sync.get_netbox_interface_type` and its view wrapper, and the table's
`_interface_type_mapping_cache` / `get_interface_mapping`. An AST test fails if code outside
`interface_rules.py`, the model, forms, filters, serializers, tables/mappings and migrations
queries `InterfaceTypeMapping`.

**Consumers.**

| path | for an `IGNORE` or `AMBIGUOUS` port |
|---|---|
| interfaces tab render | Every port is enriched first, so a bound ignored port still counts as matched and never makes its interface NetBox-only. Name decisions keep ignored ports as input. Then rows are filtered before pagination. `interface_diff` gets `ignored` / `ambiguous` states with no actions. Ambiguous rows stay visible and name the rules. |
| bulk and single-row sync POST | Skip the row with a reason naming the rules. Column exclusions do not bypass it. |
| auto-select of related ports | Never adds an ignored port and does not walk through one. |
| bulk and single LAG / parent / bridge edges | Skip the edge when either end is ignored or ambiguous. The existing link stays. |
| single-row verify (VC member switch) | Re-evaluates with the new owner's platform. |
| Rebind | Refuses an ignored or ambiguous port. |
| IP tab | Create: refused. Assign or reassign to an existing interface bound to, or matched from, an ignored port: skipped with the reason, including force and primary-IP requests. Existing IPs stay. |
| cables tab | Create, tag or replace a cable where the local or the remote port is ignored or ambiguous: skipped with the reason. The far-end create is not offered and its POST is refused. Existing cables stay. |
| module port binding | Out of scope, follow-up. |

**Table UI.** `interfaces_show_ignored=1` includes ignored rows, greyed, with the rule, no
checkbox and no actions. The "N ignored" count covers the whole snapshot. The paginator links carry
the flag. The toggle resets to page 1. Off-page selections are not pruned in JS: the server gate
refuses them and reports the skip.

**Mapping UI and API.** The form, import (platform by slug), filter set and form, list table,
detail template, YAML export and serializer gain `action`, `platform` and `name_pattern`, with
`select_related("platform")`. The docs page and the contrib YAML examples are updated.

**Tests** (real DB, real requests, no mocks): model and constraint cases; `decide()` matrix
including the legacy speed matrix (condition 4); tab GET hide/toggle/count on P and Q devices
(condition 1); forged bulk and single POST (condition 2); name/platform Set GET+POST agreement
(condition 3); tie, both orders (condition 5); IP create/assign skip; cable create/replace skip;
edge skip both directions; one mapping query per request; the AST guard; an e2e toggle check.

### Round 1 (codex `gpt-6-astra` xhigh, read-only, HEAD `4345c07671`): NOT RATIFIED

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 1 | BLOCKER | `sync_interface` runs its own resolvers (`get_or_create`) before the gated updater, so the gate fires after creation | confirmed: `views/sync/interfaces.py:1479` (device), `:1519` (VM), updater at `:1547` | accepted, r2-1 |
| 2 | BLOCKER | cable rows carry only IDs and names; IP port record can be `None` | confirmed: `cables_view.py:867` row keys, `:975` reduces records to id + names; `ip_addresses_view.py:372` | accepted, r2-2 |
| 3 | MAJOR | a forced cable replacement deletes a cable whose other end is an ignored port | `cables.py:591` deletes every cable in `to_remove` | accepted, r2-3 |
| 4 | MAJOR | LAG/parent promotion overwrites a winning Set type | `_promote_lag_aggregate` / `_promote_parent_child` persist outside the attribute writer (`interfaces.py:2011`, `:2033`) | accepted, r2-4 |
| 5 | MINOR | AST allowlist misses mapping CRUD and API views | 9 queries in `views/mapping_views.py`, 1 in `api/views.py` | accepted, r2-5 |

Reviewer checks that passed: filtering before pagination is compatible (`interfaces_view.py:808`,
`:895`, `:913`); 1,024 pure-Python comparisons show the rank tuple matches the legacy speed
selector; the four partial constraints are sound; the module-binding deferral does not affect the
core's correctness or authorization; the file projection is 93 including this record.

### r2 (changes from r1, verbatim)

- **r2-1.** The gate is a rule, not two call sites: *every port-driven write path evaluates the
  decision after it resolves the owner and before its first write.* Paths: `sync_interface` (before
  `_resolve_device_interface` / `_resolve_vm_interface`), `resolve_or_create_interface_from_port`,
  `update_interface_from_port` (a second line of defence), Rebind (`interfaces.py:1763`), bulk and
  single relationship persistence (`:2102`), cable far-end create (`cables.py:1384`), cable apply
  (`cables.py:274`), IP assign (`ip_addresses.py:1141`).
- **r2-2.** Evidence and ambiguity scope.
  - `AMBIGUOUS` blocks only interface writes (create, update, promote, far-end create, IP-driven
    create). It does not block an IP assignment or a cable on an existing interface: the type is
    not written there.
  - New `matcher.may_ignore(platform_id) -> bool`: true when any Ignore rule is global or scoped
    to that platform.
  - A consumer without the full port record (`ifName`, `ifDescr`, `ifType`, `ifSpeed`) refuses
    the write with a "refresh" reason when `may_ignore` is true, and proceeds when it is false.
    Missing evidence never becomes `UNMAPPED` when an Ignore rule could apply.
  - The cables tab keeps `ifType` and `ifSpeed` next to the names in the local and remote port
    records it caches (`cables_view.py:975` and the local port map), and the rows and replay
    whitelist (`cables_view.py:316`) carry them.
  - The IP tab already has full records. A `None` record falls under the refusal rule.
- **r2-3.** Before a confirmed cable replacement deletes a cable, it checks each far termination
  of every cable in `to_remove` that is an interface bound to a LibreNMS port on this server. The
  port record is fetched by ID on this confirmed path, before the transaction. Ignored: the
  replacement is refused. Fetch failure and `may_ignore(owner platform)`: refused. An unbound
  interface is not a LibreNMS port, so no rule applies.
- **r2-4.** A promotion never overrides a `SET_TYPE` decision. If the winning type is not
  compatible with the edge (an aggregate not `lag`, a child the parent pass would promote), the
  edge is refused with a reason naming the rule. `UNMAPPED` ends keep today's promotion.
- **r2-5.** The AST guard allows `views/mapping_views.py` and `api/views.py` as management
  consumers and still forbids run-time selection anywhere else.
- **r2-6** (from the reviewer's notes). Nullable optional fields also get `blank=True`. The
  matcher's one speed reader replaces the table's `ifSpeed` default of 0 and the writer's None, so
  the two readers can no longer disagree. The browser's related-row traversal
  (`librenms_sync.js:1275`) skips rows marked ignored.

### Round 2 (codex `gpt-6-astra` xhigh, read-only): NOT RATIFIED

Round-1 findings: 1 CLOSED, 2 NOT CLOSED, 3 NOT CLOSED, 4 CLOSED (original overwrite), 5 CLOSED.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 2.1 | MAJOR | `may_ignore=False` lets an interface write go ahead on partial evidence, which can hide a Set tie (a type selector fails to match on a missing `ifType`) | follows from the r2-2 rule; the far-end create falls back to `other` today (`cables.py:1289`, `:1304`) | accepted, r3-1 |
| 2.2 | MAJOR | a manual cable pick targets an interface bound to another port, but the evidence stays the advertised port's | `cables_view.py:3553` stores only `manual_remote_id`; `:2531` keeps the advertised port identity | accepted, r3-2 |
| 2.3 | MAJOR | the displaced endpoints of a replacement are not locked; their binding or owner platform can change after the check | `_group_terminations` locks only the proposed pair (`cables.py:420`); the fingerprint has no binding or platform (`cables.py:73`) | accepted, r3-2 |
| 2.4 | MAJOR | refusing a promotion does not undo the attribute pass's type write, so an aggregate with members can lose `lag` | attribute pass runs first (`interfaces.py:219`); the updater writes type with no role check (`interface_sync.py:149`) | accepted, r3-3 |

The cable mechanism has now produced findings in both rounds (1.2, 1.3, 2.2, 2.3). r3 briefs the
general fix, not the instances, and round 3 asks for two verdicts.

### r3 (changes from r2, verbatim)

- **r3-1. Evidence rule by write kind.**
  - *Interface writes* (create, update, promote, IP-driven create, far-end create) need the
    complete record (`ifName`, `ifDescr`, `ifType`, `ifSpeed`) for the port. Without it they refuse
    with a "refresh" reason. `may_ignore` is never used to allow an interface write. The far-end
    create refuses when its port fetch returns nothing. Today it falls back to `other`.
  - *Writes on existing interfaces* (IP assign, cable create/tag/replace) can only be blocked by
    Ignore. Without the record they refuse when `may_ignore(platform)` is true and proceed when it
    is false.
- **r3-2. Cable evidence identity (general rule).** A cable write is allowed only when every
  LibreNMS port it touches is decided "not ignored". The touched ports are:
  - the row's local and remote port IDs;
  - the port bound (`librenms_id` on this server) to each NetBox termination the write attaches,
    including a manual pick;
  - the port bound to each far termination of every cable it removes.

  Records come from the cache or a fetch by ID before the transaction. Inside the transaction, the
  apply path locks every touched interface and its owner (`select_for_update`). It re-reads each
  binding and owner platform under that lock and decides with those values. When a bound port has
  no pre-fetched record, the r3-1 rule for existing interfaces applies. An interface with no
  binding on this server adds no port.
- **r3-3. A Set type never breaks an existing role.** Before `update_interface_from_port` writes a
  `SET_TYPE` result, it checks the interface's current role. An aggregate with members must stay
  `lag`. A child that has a parent keeps the plugin's promoted type. On a conflict it keeps the
  current type and reports "type kept: rule R conflicts with <role>", in the same way as the
  existing "kept its current name" message. Other fields still sync. Together with r2-4,
  promotion never overrides a Set type, and a Set type never breaks a relationship.

### Round 3 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED, B RATIFIED

Round-2 findings 2.1 to 2.4: all CLOSED.

**Verdict B: RATIFY r3-2.** Acceptance predicate: before a cable create, tag or replace, every
advertised, attached and displaced port is decided using the binding and owner platform read under
lock, with the r3-1 missing-evidence rule. First increment: the shared pre-write gate. DB tests
prove that an ignored manual or displaced endpoint stops the whole operation, including when a
binding or platform changes concurrently.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 3.1 | MAJOR | r3-3's keep-type branch lives only in the writer. The table's diff still compares the requested type, so it offers a Sync that can never clear the difference | `_type_differs` compares `context.netbox_type` directly (`interface_diff.py:157`); the table feeds the requested type (`tables/interfaces.py:254`) | accepted, r4-1 |

The reviewer states that NetBox's own `Interface.clean()` LAG/parent rules are unverified here.
r3-3 does not depend on them: it protects the plugin's own role convention.

### r4 (changes from r3, verbatim)

- **r4-1. One planned-type function.** `interface_rules.planned_type(decision, role) ->
  PlannedType(value, kept_reason)` decides the type a sync writes. `role` is
  `InterfaceRole(current_type, has_members, has_parent)`. The writer (`update_interface_from_port`)
  and the row diff (`interface_diff`) both call it. The diff compares `value` with the current
  type. A `kept_reason` renders as a note on the Type cell ("type kept: rule R conflicts with
  <role>"). It is not a difference, so it does not make the row differ or offer Sync. Other fields
  keep their own verdicts.
  - Role inputs, table: `has_members` comes from the lookup maps the tab already builds. Every
    member's interfaces are loaded, and a LAG never spans devices outside a VC, so it is the set of
    loaded `lag_id`s. `has_parent` comes from the loaded `parent_id`. No extra query per row.
  - Role inputs, writer: read from the interface under the existing lock.
  - Tests: GET, then POST, then GET for an aggregate with members and for a promoted child. After
    the POST the row shows no difference and the note.

### Round 4 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED

3.1 NOT CLOSED. Both new findings belong to the role-preservation mechanism (r3-3, r4-1), which
also produced 3.1.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 4.1 | MAJOR | `has_members` derived from loaded `lag_id`s is incomplete: a member left on a device removed from the VC, and the verify repaint's bounded candidate index (`interface_relationships.py:392`) | the verify path builds a bounded index, not the full tab maps (`views/object_sync/devices.py:299`) | accepted, r5-1 |
| 4.2 | MAJOR | `InterfaceRole` cannot express "is a LAG member"; NetBox rejects `virtual` with a LAG (upstream `device_components.py`, 4.4.0 L856, 4.6.5 L1002) and the writer saves without validation (`interface_sync.py:197`) | writer save path confirmed; NetBox source cited by the reviewer from upstream | accepted, r5-1 |

**Operator decision (2026-09-23):** NetBox's own `Interface.clean()` decides whether a planned type
fits the interface's existing links. The plugin keeps no copy of NetBox's type rules.

### r5 (changes from r4, verbatim)

- **r5-1. `planned_type` asks NetBox.** Replaces r3-3's role model and r4-1's `InterfaceRole`.
  `interface_rules.planned_type(decision, interface) -> PlannedType(value, kept_reason)`:
  - No interface (a create), or a decision that is not `SET_TYPE`: today's behaviour (the rule's
    type; an unmapped create gets `other`; an unmapped existing interface keeps its type).
  - `SET_TYPE` equal to the current type: `value` is the current type.
  - `SET_TYPE` different from the current type: validate an in-memory copy with the new type
    through the plugin's existing guarded `clean()` path (the NetBox 4.4.0 parent-chassis
    tolerance, `utils.netbox_clean_reads_parent_virtual_chassis`, `views/sync/interfaces.py:2071`).
    If it passes: `value` is the new type. If it fails, the copy is validated again with the
    current type. Only errors that the new type adds count. Then `value` is the current type and
    `kept_reason` is NetBox's message. Errors that exist with the current type too do not block
    the type.
  - It reads the persisted relationships through NetBox, so the result does not depend on page
    scope or on the verify repaint's candidate set. The writer, the full-tab diff, the verify
    repaint and the single-row sync response all call it. The writer calls it under the existing
    row lock.
  - Cost: `clean()` runs only for rows with an existing interface whose Set type differs from the
    current type. In a steady state there are few such rows.
- **r5-2. Promotion (replaces r2-4).** A relationship pass never changes an end whose decision is
  `SET_TYPE`. It links without promotion when NetBox's `clean()` accepts the link with that type,
  and refuses the edge with NetBox's reason when it does not. `UNMAPPED` ends keep today's
  promotion.
- r3-3 and r4-1 are withdrawn. 3.1, 4.1 and 4.2 are to be judged against r5-1.

### Round 5 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED — blocker cap reached

3.1 CLOSED, 4.1 CLOSED (withdrawn model), 4.2 NOT CLOSED (5.1). r5-2 promotion coverage: no
omission (four sites confirmed by AST).

| # | sev | finding | status |
|---|---|---|---|
| 5.1 | MAJOR | NetBox's `clean()` stops at its first error. An unrelated bridge error found first hides the virtual-with-LAG error, so comparing the errors of the two copies lets an invalid type through (NetBox 4.4.0 L837-858, 4.6.5 L983-1004) | open |
| 5.2 | MAJOR | the 4.4.0 guard re-raises `AttributeError` when the chassis no longer matches (`views/sync/interfaces.py:2078`), so a table render can fail | open |
| 5.3 | MAJOR | the attribute writer locks owners, not the interface row (`:1039`, `:1238`, resolver `:1445`); a concurrent LAG assignment is overwritten by a stale save (`interface_sync.py:198`). The gap exists today | open |

Clustering: 2.4, 3.1, 4.1, 4.2, 5.1, 5.2 and 5.3 all sit in one mechanism: **checking an
attribute-pass type write against the interface's existing links**. Today's writer has the same
gap for legacy `ifType` mappings: it writes a mapped type with no check against the links.

**Proposed split line (operator decision pending):**

- *Core:* everything ratified or not contested, plus a minimal promotion rule. The relationship
  pass does not promote an end whose decision is `SET_TYPE`. It tries the link, and the `clean()`
  that the relationship pass already runs today (`views/sync/interfaces.py:2019`, `:2071`) accepts
  or refuses it. The attribute pass writes the planned type as today's writer does. No new
  type-vs-links check.
- *Deferred mechanism:* validating an attribute-pass type write against existing links, for
  legacy and new rules alike, including the interface row lock (5.3). This goes to a follow-up
  issue with findings 2.4 to 5.3 as its evidence.
- Standalone check: the core's acceptance conditions 1 to 5 do not need the deferred part. The
  core leaves today's gap as it is; it does not add a new kind of write.

**Operator decision (2026-09-23): split.** The core goes to one counted verdict round. A blocker
there reports the design blocked. The design does not split again.

### Core (split scope, for the verdict round)

The core is r1 with these changes: r2-1, r2-2 as narrowed by r3-1, r2-5, r2-6, r3-1, and r3-2
(already RATIFIED as verdict B). r2-3 is replaced by r3-2. r2-4, r3-3, r4-1 and r5-1 are
withdrawn. r5-2 is replaced by this rule:

- **Core promotion rule.** The relationship pass (bulk LAG `:755`, bulk parent `:760`, single LAG
  `:2527`, single parent `:2548`) never promotes an end whose decision is `SET_TYPE`. It tries the
  link with that end's current type. The `clean()` it already runs accepts or refuses the link, and
  a refusal is reported with NetBox's reason. The exclusion and permission checks tied to promotion
  apply only when a promotion is actually planned. `UNMAPPED` ends keep today's promotion.
- **Attribute pass.** A `SET_TYPE` result is written as today's writer writes a mapped type: no
  check against existing links, no interface row lock. That is today's behaviour for legacy
  mappings, and it is the deferred mechanism.
- **Diff.** `interface_diff` compares the decision's type (the value the writer writes) with the
  current type. Since the writer applies it unconditionally, the two cannot disagree.

**Deferred (follow-up issue, filed after the core is implemented):** validate an attribute-pass
type write against the interface's existing links, for legacy and new rules alike, with the
interface row lock. Evidence: findings 2.4, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3. Module port binding is
a second follow-up.

### Core verdict round (codex `gpt-6-astra` xhigh, read-only): RATIFY core

No findings. The core stands alone. The promotion rule covers all four sites (AST). Acceptance
conditions 3 and 5 stay consistent. No finding outside the deferred cluster reopens. 7,168
in-memory cases agree with the legacy selector.

Ratified scope: r1 + r2-1, r2-2 as narrowed by r3-1, r2-5, r2-6, r3-1, r3-2, and the core
promotion, attribute-pass and diff rules. r2-3 is superseded. r2-4, r3-3, r4-1 and r5-1 are
withdrawn. r5-2 is replaced.

## 5. Next action

1. **Increment 1:** the model extension and migration `0022`, `interface_rules.py` (matcher,
   `RuleDecision`, `may_ignore`, request cache), mapping form, filter, table, serializer and
   import/export. Acceptance: legacy selections unchanged; platform and raw-name matching; Ignore
   precedence; explicit equal-rank ambiguity; invalid rules rejected; one rule query per request
   and none per decision.
   *Implemented (uncommitted), codex implementation review CLEAN after 4 rounds.* Decisions made
   during review:
   - A CSV import that has a `name_pattern` column is refused as a whole. NetBox's `parse_csv`
     strips every cell, so whitespace in a regex cannot survive CSV. Re-reading the raw CSV was
     tried and dropped: a second parser kept diverging from NetBox's (trailing space, sniffed
     `skipinitialspace`, normalized headers). YAML, JSON, the form and the API keep patterns
     verbatim.
   - `utils.REGEX_COMPILE_ERRORS = (re.error, OverflowError)`; a pattern such as `a{4294967295}`
     is a validation error, not a crash.
   - A background-job CSV *upload* fails upstream in NetBox 4.4-4.6 for every model (the file is
     read before the job replays it). Out of scope.
2. **Increment 2:** interfaces tab and its writers (gate before the resolvers, hide + toggle,
   auto-select, relationship promotion rule, Rebind, verify repaint). Acceptance conditions 1 to 5.
   *Implementation finding (increment 2):* the core promotion rule assumed the relationship pass's
   `clean()` refuses a LAG link to a non-`lag` aggregate. NetBox 4.4.0 and 4.7 do not check the
   aggregate's type, and migration 0020 seeds `ieee8023adLag -> lag`, so every aggregate decides
   `SET_TYPE(lag)`. Applied literally, no aggregate would ever be promoted. **Revised rule:** a
   promotion runs only when its target type equals the end's own decision type. When an end's
   decision is `SET_TYPE` with a different type, the edge is refused and the existing link stays.
   `UNMAPPED` ends keep today's promotion. "Promotion never overrides a Set type" still holds.
   *Operator decision (2026-09-24), from increment-2 review round 4:* when a stored VC-member
   choice for a rendered row differs from the member the page renders, the browser does not
   restore it. It drops that stored selection and shows a notice. The async "pending restore" was
   removed: its lifecycle produced findings in three rounds in a row (dependency selection,
   deselect-all, htmx swap, navigation). Off-page selections still submit with their stored member,
   and the server decides.
3. **Increment 3:** IP tab and cables tab gates (r3-1, r3-2).
   *Implemented (uncommitted).* Decisions made during implementation:
   - One check for writes on existing interfaces: `matcher.check_existing_interface_write`
     returns the blocking IGNORE or INCOMPLETE decision, or None. The IP writer, the IP table, the
     cable gate and the cable table all call it.
   - IP: the assignment touches the IP row's source port and the port bound to the target
     interface, re-read from the locked row (a name match can reach an interface bound to another
     port). Both are decided with the platform of the owner that `_lock_target_interface` locked.
     The IP table uses the same port list (`ip_assignment_ports`) with the snapshot records. Both
     resolve rows against one scope (`ip_interface_scope`): the object and the chassis members
     the caller may view, and their interfaces the caller may view. The sync locks the same
     owners, so the table and the sync agree, and neither reaches a hidden member.
   - The IP snapshot keeps `bound_ports_by_id`: the records of the ports bound to the interfaces
     in scope, read from the same device-ports payload. They are evidence for the rules only and
     never become IP rows or name candidates (`ports_by_id` is unchanged).
   - One disclosure rule for every refusal and pill that can name a port (`utils.PortDisclosure`,
     used by the cable table and writer and the IP table and writer): a port's id, name and rule
     label are shown only when its owner is in the caller's view scope. For a port bound on this
     server, the owner is the bound interface's own device or VM, read from the binding, and the
     interface must be visible too; the owner a caller passes counts only for an unbound port.
     An owner that did not resolve is not visible, an ambiguous binding is not visible, and no
     port is visible by default, the row's own ports included. A port that is not visible still
     blocks, with `HIDDEN_PORT_REASON`, which names no port, name or rule.
   - A table preloads the rule once for every port it may ask about (one binding query per
     interface model, one view-scope query per model), so a render costs the same queries for
     2 or for 20 blocking rows. A writer asks one row at a time through the same rule.
   - Cables: the snapshot keeps `local_port_record` (from the device ports payload) and
     `remote_port_record` (from the neighbour's port index, now cached under a new key so an entry
     without records is never read). The row's remote port is `remote_port_key`, and also
     `remote_port_id` when it differs. Each port is decided with its own owner's platform
     (`port_owner_id`): the local port with the local owner, and a bound port with its
     interface's device. The advertised remote port uses the resolved neighbour (its chassis
     member for a VC); a manual pick onto another device never changes that. The gate locks these
     owners with the others.
   - *Operator decision (2026-09-24):* when the advertised port's owner does not resolve, the port
     is decided with no platform (`platform_id=None`), so only global rules match it, as for a
     device with no platform (r1). A manual pick does not change this; the picked interface's own
     bound port is decided separately with the picked device's platform. A missing record follows
     r3-1 with no platform, so only a global Ignore rule refuses it.
   - The far ends of the cables on both endpoints are candidate evidence before the lock. Their
     interfaces and owners are locked in the same statements as the endpoints and owners, so the
     lock order does not change. A removed cable that ends on an interface the lock did not take
     makes the row stale.
   - Records are fetched by id before the row's transaction, and only when the snapshot has an
     Ignore rule. The table decides only the ports whose records the snapshot holds (a render
     makes no LibreNMS call); the sync gate decides every touched port. A blocked row loses Sync
     Cable and the far-end create, and shows the rule.
   *Known limitation (increment-3 review round 5):* `PortDisclosure` finds bindings through
   `build_librenms_id_qs`, which matches only ASCII-digit ids. A row whose stored `librenms_id`
   is a reader-only form (for example `"7_101"`, accepted by `get_librenms_device_id`) is not
   found, so its port counts as unbound and its refusal can be named. Every guard built on
   `find_by_librenms_id` shares this split. It is deliberate (narrowing the reader wipes mappings);
   the only lossless fix is a heal path that normalises such values. That is an open follow-up.
4. After the core is implemented: file follow-ups for type-vs-links validation (findings 2.4 to
   5.3) and module port binding.

## 0. Refuted claims

None. Every reviewer finding was confirmed and accepted. Withdrawn design statements are
listed in the ratified scope above.

## 6. Follow-up: interface type vs existing links (issue #185)

### Brief

**Decision.** How an interface sync decides whether the type it plans to write fits the
interface's persisted links, the same way for the row diff and for the writer, for legacy `ifType`
mappings and Set type rules alike. The writer locks the interface row and plans with the locked
state.

**Problem as a class.** The attribute writer (`update_interface_from_port`,
`interface_sync.py:86`) writes `rules.decide_interface_write(...).netbox_type` with no check against
the interface's links, then calls a full `save()` (`:198`) outside any interface row lock. The row
diff (`interface_diff._type_differs`, `:171`) compares the same decision type. Three failures follow:
a non-`lag` type on an aggregate with members; a type NetBox's own `clean()` rejects (for example
`virtual` on a LAG member) stored without validation; and a stale full save overwrites a concurrent
link write (lost update). The module that owns the rule is "the type a sync writes to an existing
interface", which today has no owner: the decision type flows straight into both the writer and the
diff.

**Operator decisions in force.** The plugin keeps no copy of NetBox's type rules (2026-09-23). Ignore
and Set type semantics, and the core promotion rule (section 4), are ratified and not open.

**Constraints.**

- Supported NetBox: `min_version = "4.4.0"`; the dev container runs 4.7.0. NetBox's
  `Interface.clean()` type rules differ by version (evidence below).
- The diff, the verify repaint (`views/object_sync/devices.py`) and the single-row sync response
  must reach the same verdict as the writer. The verify repaint builds a bounded candidate index,
  not the full tab maps (finding 4.1).
- A table render must not raise (finding 5.2). No per-row query in the steady state.
- No broad exception swallowing; fail closed on unexpected state.
- PR #180 is at 97/100 changed files. The main files are already in its diff.

**Observable acceptance conditions.**

1. An aggregate with at least one LAG member, whose planned type is not `lag`, keeps `lag` after a
   sync. The row shows why and does not show a type difference. Its members keep `lag_id`.
2. A LAG member whose planned type is `virtual` keeps its type after a sync; the stored pair is one
   NetBox's `clean()` accepts. Same for a child with a parent whose planned type is not `virtual`.
3. The verdict is the same for a legacy mapping and a Set type rule with the same type.
4. The table (GET), the sync (POST) and the table again (GET) agree for cases 1 and 2, including the
   verify repaint.
5. A concurrent link write between the table load and the sync is not lost, and the sync plans with
   the link it sees under the lock.
6. An interface with no links, or a create, behaves as today.
7. A table render on NetBox 4.4.0 with a cross-chassis parent does not raise.

**Evidence.**

- NetBox 4.7.0 `Interface.clean()` (`dcim/models/device_components.py`) and
  `InterfaceValidationMixin.clean()` (`dcim/models/mixins.py:142`): virtual types with a cable or
  `mark_connected`; a parent only on a `virtual` or channel subinterface; `virtual` with a LAG;
  channel rules; wireless-only fields. NetBox 4.4.0 L842-865: the same cable rules, "Only virtual
  interfaces may be assigned to a parent interface", and the parent-chassis dereference bug at L875.
  `clean()` raises on the first error only.
- The `lag` FK has no `limit_choices_to` in 4.7.0: no NetBox model rule ties an aggregate's type to
  its members. The relationship pass promotes the aggregate itself (`views/sync/interfaces.py:2210`).
- Writer call sites: `SyncInterfacesView.update_interface_attributes` (`views/sync/interfaces.py:1722`),
  `resolve_or_create_interface_from_port` (`interface_sync.py:289`). Owner locks, not interface
  locks: `views/sync/interfaces.py:1371`, `:2095`, `:2104`.
- The guarded `clean()` for the 4.4.0 bug: `_validate_relationship` (`views/sync/interfaces.py:2275`),
  `utils.netbox_clean_reads_parent_virtual_chassis`.
- Prior findings 2.4, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3 (section 4), and the two rejected shapes: a
  plugin-side role model (4.1, 4.2), and comparing `clean()` errors of a new-type copy against a
  current-type copy (5.1).

**Candidate shapes.**

- **S1, NetBox judges one copy, fail closed.** Validate an in-memory copy with the planned type
  through the guarded `clean()`. Any error keeps the current type, with NetBox's message. Add the
  plugin's own "aggregate with members stays `lag`" rule, which is not a NetBox rule.
- **S2, plugin-owned link facts and rules.** Read link facts from the DB and apply a plugin rule
  table. Needs the operator decision above to be reopened.
- S3 (the second designer's).

### 6A. Claude design (Opus 5.5, from the brief; drafted before reading 6B)

**Owner.** `interface_diff.py` already owns "what the writer writes" (the shared field schema,
`synced_description`, `interface_enabled_from_port`) and the writer imports it. It gains one function:

    planned_interface_type(interface, decision) -> PlannedType(value: str | None, kept_reason: str | None)

- `interface` is a persisted `Interface` (a `VMInterface` has no type: returns `(None, None)`, no queries).
- `decision.netbox_type is None` (UNMAPPED): today's rule. Current type, or `other` when empty. No check.
- `decision.netbox_type == interface.type`: `(interface.type, None)`. No query.
- Otherwise, in this order, and the first refusal wins:
  1. **Plugin rule (not a NetBox rule):** `Interface.objects.filter(lag_id=interface.pk).exists()`
     and the planned type is not `lag` -> kept, "type kept: the interface has LAG members; <rule> sets <T>".
     NetBox has no model rule for this (the `lag` FK has no `limit_choices_to`), so it does not
     conflict with "no copy of NetBox's type rules".
  2. **NetBox judges one copy:** `copy.copy(interface)`, set `type = T`, run the guarded clean
     (below). A `ValidationError` -> kept, "type kept: NetBox refuses <T>: <first message>".
  3. Else `(T, None)`.
- **No comparison with a current-type copy.** Any `clean()` error keeps the current type. This closes
  5.1 by construction: an error that hides the type conflict still keeps the type. The price is that
  an unrelated pre-existing NetBox error on the interface also holds a type change. The reason shows
  NetBox's message, so the operator sees what to fix. Fail closed, as the brief requires.
- It reads persisted links through NetBox and one `exists()`, never the page maps, so it does not
  depend on page scope or the verify repaint's candidate index (closes 4.1). It needs no role model
  (closes 4.2: NetBox's own `virtual`-with-LAG and parent rules run on the copy, per version).

**Guarded clean, shared.** `_validate_relationship`'s 4.4.0 tolerance moves to
`utils.netbox_interface_clean(interface)`, used by the relationship pass and by the planner. Fix for
5.2: when the `AttributeError` is the known 4.4.0 `virtual_chassis` dereference, the answer is known.
Same chassis: rerun without the parent (today). Different or no chassis: raise the `ValidationError`
NetBox meant ("belongs to <device>, which is not part of virtual chassis <vc>"). Any other
`AttributeError` still propagates (a real defect, fail fast). So a table render never raises for the
4.4.0 bug.

**Writer (`update_interface_from_port`).** For an existing interface (`pk` set):

1. Require an open transaction (`transaction.get_connection().in_atomic_block`, else raise).
   Callers already lock the owner device or VM first; the interface row is locked after it, which
   keeps the owner -> interface order the relationship pass uses.
2. `type(interface).objects.select_for_update(of=("self",)).filter(pk=interface.pk)` and
   `interface.refresh_from_db()`, so the plan reads the locked links (closes 5.3's stale read).
3. `planned_interface_type(interface, decision)` sets the type. A kept reason is logged at info.
4. Save with `update_fields` = the tracked fields that changed plus `custom_field_data`, not a full
   `save()`. A concurrent write to `lag`, `parent`, `bridge` or any untracked column is never
   overwritten (closes the lost update, for every field, not only type).

A create (no `pk`) has no links: today's behaviour.

**Diff.** `compute_row_sync_state` calls `planned_interface_type` once per existing row and puts
`value` in `DiffContext.netbox_type`. `_type_differs` compares it. `RowSyncState` gains
`type_kept_reason`; the Type cell renders it as a note. A kept type is not a difference, so a row
whose only difference is a kept type is in sync and offers no Sync. The table, the verify repaint and
the single-row sync response all go through `compute_row_sync_state`, so they agree with the writer.

**Cost.** Queries only for a row whose decision type differs from its current type: one `exists()`
and the queries `clean()` makes (cable, lag, parent, bridge, untagged VLAN; 2 to 5). A kept row pays
this on every render. Kept rows are a misconfiguration signal and few; the cost is stated, not hidden.

**Validation.**

- DB tests on `planned_interface_type`: aggregate with members + non-`lag` (legacy mapping and Set
  rule, AC1, AC3); member + `virtual` (AC2); child with parent + `1000base-t` (AC2); unrelated
  pre-existing error holds the type (the 5.1 disposition); no links (AC6).
- End to end GET -> POST -> GET on the interface tab for AC1 and AC2, plus the verify repaint (AC4).
- Lost update (AC5): load the tab, set `lag_id` on the row in the DB, POST; `lag_id` survives and the
  type is planned with it. Mutation check: revert `update_fields` to a full save, the test goes red.
- 4.4.0 guard (AC7): version patched to 4.4.0 (the version read is the one mocked boundary), a
  cross-chassis parent on a real VC, table render -> kept note, no exception.

### 6B. Codex design (`gpt-6-astra`, effort high, read-only, blind)

Input: the brief above only (constraints, acceptance conditions, evidence), fresh context, told not to read "Candidate shapes". It reports reading sections 1-5 only. Caveat: S1's one-paragraph summary was in the file it could open, so independence is not guaranteed. Full text: kept in the session scratchpad; the load-bearing points are in the divergence table.

Summary: new module `interface_type_plan.py`; `load_type_evidence(interfaces)` batch-loads persisted facts, and a pure `plan_interface_type(decision, interface, evidence) -> TypePlan(value, status accepted|unchanged|kept|blocked, reason)` does no queries. A private materialized relation reader answers NetBox 4.7's channel `aggregate(Max)` from batched data. Members `Exists` rule; one copy through the real `clean()`; any error keeps the type; no two-copy comparison. 4.4.0 fault -> `kept` always, no retry. Writer reselects under a row lock, recomputes the decision, full-saves the fresh instance and returns `InterfaceWriteResult(interface, changed, type_plan)`. Batch paths lock every interface row in pk order before mutation. `blocked` for missing evidence or an unexpected validator `AttributeError`. Unlinked fast path. AST guard so no type write bypasses the module.

### 6C. Divergence table

| # | decision | 6A (Claude) | 6B (codex) | evidence | disposition | consequence |
|---|---|---|---|---|---|---|
| D1 | who reads the evidence | the planner queries per row, only when the decision type differs from the current type | batch `load_type_evidence`; pure planner with zero queries; a fake ORM reader for 4.7's channel `aggregate(Max)` | 4.7 `clean()` makes its own DB reads (`cable_terminations.exists()`, channel max); a zero-query planner has to answer them from fakes | **6A.** The fake relation reader re-implements a Django related manager to feed NetBox, and codex names it the highest-risk element. Its only gain is query count on refused rows. The cost of 6A is stated: 2-5 queries per row whose type differs, every render. | a query-count test pins 0 extra queries for rows whose type matches |
| D2 | module | `planned_interface_type` in `interface_diff.py` (already the shared writer/diff schema, imported by the writer) | new `interface_type_plan.py` | #180 is at 97/100 files; `interface_diff.py` is in the diff | **6A.** Same seam, no new file. | none at call sites |
| D3 | 4.4.0 parent-chassis fault | shared guard: same chassis, rerun without the parent; different chassis, raise NetBox's intended `ValidationError` | always `kept` ("this NetBox version could not validate"), no retry | 4.4.0 L864 (the virtual-only-parent rule) runs before the L868-880 device/chassis block, so the fault at L875 means L842-865 already passed; the rerun keeps the later LAG/bridge/wireless/VLAN checks | **6A.** The retry loses no type rule. Codex's version holds a correct type change on 4.4.0 forever. | AC7 test: same-VC child accepts a valid type; mismatched VC keeps with NetBox's message |
| D4 | writer freshness | lock the row, `refresh_from_db()` the passed instance, then decide and plan | reselect a new instance, full save it, return `InterfaceWriteResult` so callers switch objects | callers pass shared-index objects that the relationship pass reuses (`_apply_interface_relationship` docstring) | **6A.** Refreshing in place keeps one object per row, so the relationship pass sees fresh values too, with no new return type. Codex's point adopted: the decision is recomputed after the lock (owner platform is re-read). Under the row lock a full save is safe; `update_fields` is dropped from 6A. | mutation check: remove the refresh -> AC5 test red |
| D5 | lock order | per row, inside the writer, after the caller's owner locks | lock every interface row of the batch in pk order before any mutation | every plugin path that locks interfaces takes owner locks first (`views/sync/interfaces.py:1371`, `:2095`; `cables.py:632` then `:648`), so plugin writers on one device serialize at the owner | **6A, contested.** Round 1 must check every interface-locking path (including `migrate.py:520`, `:838`) for an owner-first order. | a counterexample path makes this a finding |
| D6 | unexpected validator `AttributeError` | propagates (defect, fail fast) | `blocked` row status | user rule: fail fast, no broad swallowing | **6A.** Only the known 4.4.0 fault is handled. | none |
| D7 | already-invalid interface (unrelated `clean()` error) | type change held, NetBox's message shown | unlinked fast path keeps today's write; linked ones held | AC6 wording "no links behaves as today" | **Operator decision (2026-09-24): hold the type (6A).** The fast path lists clean()'s inputs (cable, wireless, channel), which is close to a copy of NetBox's rule inputs. | AC6 reads: an interface whose new-type copy passes `clean()`, or a create, behaves as today |
| D8 | kept message on POST | info log; the swapped table shows the note | warning log plus a collected sync message | the htmx swap re-renders the row with its note | **6A**, log at warning. | none |
| D9 | AST guard | none | no attribute type write or type comparison bypasses the planner; ratified promotions allowed | user rule: prevent the class mechanically | **6B adopted.** | a new test in the existing AST guard file |
| D10 | tests for AC5 | sequential stale-object test | threaded, independent connections, barriers | `transaction=True` flushes seeded rows (known harness trap) | **both:** sequential test required; one threaded lock-wait test if the harness allows it | |

Both designs agree: members `exists()` as the one plugin rule; one copy through the real `clean()`;
any error keeps the type; no two-copy comparison; the diff compares the planned value; a kept type is
not a difference.

### r1 (merged candidate)

- **Planner.** `interface_diff.planned_interface_type(interface, decision) -> PlannedType(value,
  kept_reason)`, as in 6A. UNMAPPED, create, VMInterface and unchanged type: today's behaviour, no
  query. A changed type: (1) members `exists()` -> kept if not `lag`; (2) guarded `clean()` on
  `copy.copy(interface)` with the new type -> kept on `ValidationError`, reason carries NetBox's first
  message and the rule names; (3) else the new type.
- **Guard.** `utils.netbox_interface_clean(interface)`, shared with `_validate_relationship`, as D3.
- **Writer.** For an existing interface: require an atomic block; `select_for_update(of=("self",))`
  on the row after the caller's owner locks; `refresh_from_db()`; decide the rule with the re-read
  owner platform; plan; set fields; full `save()`. Kept reason logged at warning.
- **Diff.** `compute_row_sync_state` plans once per existing row; `_type_differs` compares
  `value`; `RowSyncState.type_kept_reason`; the Type cell renders the note. Table, verify repaint and
  single-row response share it.
- **AST guard** (D9).
- **D7 (operator, 2026-09-24):** an already-invalid interface holds the type change, with NetBox's message.
- **Tests:** 6A's list plus D10.

### Round 1 (codex `gpt-6-astra` high, read-only): r1 NOT RATIFIED

D3 held (the reviewer ran real 4.4.0 `clean()`: physical-parent rejection before the fault,
same-VC acceptance after the retry, virtual-with-LAG rejection on the retry). Writer callers are
atomic and owner-locked. Render parity has one seam. D1 stands.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 1.1 | MAJOR | D5 premise false: module type apply locks interfaces in `(name, pk)` order with no device lock, so it can deadlock against a per-row writer; module adoption and migrate reconciliation also lock interfaces before (or without) the owner | confirmed: `views/sync/modules.py:2518` (no Device lock in the view), `:483`; `views/sync/migrate.py:321` locks an interface that can sit on a third device | accepted, r2-1 |
| 1.2 | MAJOR | `refresh_from_db()` does not reset NetBox 4.7's `_original_name`, so a restored name skips the channel-child rename cascade | confirmed: `dcim/models/mixins.py:274` sets it in `__init__`; `save()` reads it at `:288` | accepted, r2-2 |
| 1.3 | MAJOR | module template type apply writes `type` directly (`full_clean(exclude=...)`), so an aggregate with members can lose `lag` | confirmed: `views/sync/modules.py:2391` | accepted, r2-3 |
| 1.4 | MAJOR | "create = no pk" is wrong: both resolvers persist a skeleton with `get_or_create` before the writer | confirmed: `views/sync/interfaces.py:1670`, `interface_sync.py:275` | accepted, r2-4 |
| 1.5 | MINOR | patching only the version does not run 4.4.0's faulty `clean()` | confirmed: CI matrix has a `v4.4.0` leg (`.github/workflows/test.yaml:29`) | accepted, r2-5 |

Stated assumption (from the review, not a finding): `copy.copy` shares related objects, prefetch
caches and `custom_field_data` with the original. Stock NetBox `clean()` only reads them.

### r2 (changes from r1, verbatim)

- **r2-1. Owner-first lock order is an invariant.** Every plugin transaction that locks interface
  rows first locks their owners (virtual chassis, then devices in pk order, or the VM). Fix the three
  sites: module type apply locks the target device first; module adoption locks `module.device`
  first; migrate reconciliation reads the interface's `device_id`, locks that device, then locks the
  interface and re-checks `device_id`. Mechanical guard: an AST test finds every `select_for_update`
  call in `netbox_librenms_plugin/` whose queryset is an interface model (or a device component
  manager); in each enclosing function an owner lock call must come first, or the site is in an
  explicit allowlist with a reason. A new unclassified site fails the test.
- **r2-2. The writer reselects, it does not refresh.** `update_interface_from_port` starts with
  `type(interface).objects.select_for_update(of=("self",)).get(pk=interface.pk)`, decides the rule
  with the locked owner platform, plans, writes and full-saves that fresh instance, and returns
  `InterfaceWriteResult(interface, changed)`. Both callers (`SyncInterfacesView.update_interface_attributes`
  and `resolve_or_create_interface_from_port`) continue with the returned instance (MAC, VLAN, the
  IP-driven create). It asserts an atomic block. D4 is reversed on this evidence.
- **r2-3. One type-change check, two users.** `interface_diff.type_change_refusal(interface, new_type)
  -> str | None` holds the members rule and the guarded `clean()` on a copy. `planned_interface_type`
  calls it, and `_apply_module_interface_type` calls it in place of its own `full_clean(exclude=...)`
  (a refusal returns `validation_failed` with the reason). The D9 AST guard allows exactly these type
  writers: the attribute writer via the planner, module apply via the check, the ratified
  promotions, and the relationship pass's restore callbacks.
- **r2-4. "No type yet" is the create case.** An interface with an empty `type` takes the planned
  type without the check. This covers the skeleton both resolvers create, with no provenance flag.
  A type-less row cannot be held at `""` (NetBox requires a type), and today's unmapped rule already
  treats "no type yet" as "fill".
- **r2-5. AC7 runs NetBox 4.4.0's real `clean()`** on CI's `v4.4.0` leg (skipped on other versions),
  and locally through the old-NetBox simulation. Cases: same VC accepts a valid type; different VC
  keeps the type with NetBox's intended message; a retry that then hits virtual-with-LAG keeps the
  type. Mutation check: remove the guard, the test goes red on 4.4.0.

### Round 2 (codex `gpt-6-astra` high, read-only): r2 NOT RATIFIED

1.2, 1.3, 1.5 CLOSED. 1.1 and 1.4 NOT CLOSED. The reviewer enumerated 52 `select_for_update` and 12
`relock_scoped_row` calls. Fresh-instance propagation: no stale write left if both callers use the
returned instance; relationship indexes are rebuilt after the attribute pass (`views/sync/interfaces.py:619`).

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 2.1 | MAJOR | r2-1's reconciliation fix locks a newly found third device while donor/winner are held, so a device-lock cycle with cable sync (`cables.py:632`) appears; `TransferDeviceIPView` (`migrate.py:960`) and `MergeNetBoxDevicesView` (`imports/actions.py:4012`) have the same lookup | accepted as a defect of r2-1, not of today's code | r3-1: out of core |
| 2.2 | MAJOR | a device lock inside module adoption conflicts with the bay/module locks its callers already hold and with NetBox's module-move order (device before bay) | accepted as a defect of r2-1 | r3-1: out of core |
| 2.3 | MAJOR | "an owner lock comes first in the function" is not a sound AST predicate (wrapper calls, lazy querysets, branches, wrong owner) | accepted | r3-1: out of core |
| 2.4 | MAJOR | replacing module apply's `full_clean(exclude=...)` with model `clean()` drops `type` field validation (choices) | confirmed: `full_clean` runs `clean_fields` on the non-excluded `type` | accepted, r3-2 |
| 2.5 | MAJOR | "empty type = create" is unsafe: a skeleton synced with Type excluded can gain a LAG in the relationship pass, and a later `virtual` fill would skip the check | accepted: the path is `views/sync/interfaces.py:1670` -> `interface_sync.py:139` -> `:770` | accepted, r3-3 |

**Clustering.** 1.1, 2.1, 2.2 and 2.3 sit in one mechanism: **plugin-wide lock order for interface
rows**. It predates #185. The relationship pass already takes owner locks and then one pk-ordered
interface batch (`interface_relationships.py:160`), while module type apply (`modules.py:2518`,
`(name, pk)`, no owner), module adoption (`:483`, no order), and the migrate/transfer/merge owner
lookups do not. Today's relationship pass is exposed to the same cycles.

### r3 (changes from r2, verbatim)

- **r3-1. Split: core A and mechanism B.** r2-1 is withdrawn.
  - *Core A (issue #185):* the writer locks its row with `select_for_update(of=("self",))` per row,
    after the owner locks every caller already holds (verified in round 1). That is the relationship
    pass's own discipline (owner first, then interface rows). Module type apply, which the core
    already changes (r2-3), locks the target device before its interfaces and orders its interface
    lock by pk. No other path changes its lock order in the core.
  - *Mechanism B (follow-up issue, filed after the core):* a plugin-wide interface-row lock order:
    module adoption vs. install/replace/move order (2.2), migrate/transfer/merge owner lookups
    constrained to the locked winner (2.1), and an inventory guard over every locking primitive with
    caller contracts and negative fixtures (2.3). Evidence: 1.1, 2.1, 2.2, 2.3.
  - Standalone check: the core adds no new kind of lock-order exposure. Its per-row locks sit under
    the owner lock, so plugin writers that take the owner lock serialize there. A path that skips the
    owner lock can already deadlock against the relationship pass today; PostgreSQL detects and
    aborts that. B removes that class.
- **r3-2. The check keeps field validation.** `type_change_refusal(interface, new_type)` runs, on the
  copy: `clean_fields(exclude=<every field except type>)`, then the members rule, then the guarded
  `clean()`. The first failure is the reason.
- **r3-3. Creation provenance, not empty type.** Both resolvers already have `get_or_create`'s
  `created`. They pass `created` to the writer (required keyword). Only `created=True` takes the
  planned type unchecked. A pre-existing type-less row is checked like any other; on a refusal it
  keeps its type and the row shows the reason. r2-4 is withdrawn.

### Round 3 (codex `gpt-6-astra` high, read-only): A NOT RATIFIED, B NOT RATIFIED

1.4, 2.4, 2.5 CLOSED. 1.1 NOT CLOSED. r3-3 provenance: exactly two production writer calls and
three `get_or_create` sites (`views/sync/interfaces.py:1670`, `:1710`, `interface_sync.py:275`); OOB
rows use the device resolver; existing-id/name branches carry `created=False`.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 3.1 | MAJOR | per-row writer locks in snapshot order (`views/sync/interfaces.py:1102`) cycle with migrate reconciliation, which locks third-device interfaces one by one before its owner check (`migrate.py:298`, `:321`); even unchanged rows now take locks; the sync catches `IntegrityError`, not a deadlock `OperationalError` (`:250`) | accepted. Note: reconciliation's field order is arbitrary, so today's pk-ordered relationship batch deadlocks with it too for the mirrored input. Any new lock adds schedules against such a counterparty, so the counterparties must be fixed in A. | accepted, r4-1, r4-2 |
| 3.2 | MAJOR | module type apply resolves the module before the transaction; after a move it would lock the old device and the new device's interfaces | confirmed: `views/sync/modules.py:2505`, `:2520` | accepted, r4-2 |

Verdict B: the clustering is valid, but A cannot defer the compatibility of its own new locks.

### r4 (changes from r3, verbatim)

- **r4-1. One interface acquisition per sync transaction.** After the owner locks and before the
  first row mutation, the bulk interface sync locks every interface row of the locked owner scope
  (the device, every member of its virtual chassis, or the VM) in one
  `select_for_update(of=("self",)).order_by("pk")` statement. The writer's per-row reselect then
  re-reads rows the transaction already holds. Relationship targets sit in the same owner scope
  (NetBox requires the same device or virtual chassis for `lag`, `parent`, `bridge`), so the
  relationship pass's later batch takes no new row locks. Rows the transaction creates are private
  to it. The IP-driven create locks its one row under the owner lock it already holds. Cost: one
  statement per sync, one row lock per interface of the owner scope.
- **r4-2. Counterparties that lock interface rows without the owner lock are fixed in A.**
  - Migrate reconciliation (`migrate.py:321`), `TransferDeviceIPView` (`migrate.py:960`) and
    `MergeNetBoxDevicesView` (`imports/actions.py:4012`): the interface lock query is constrained
    to the already-locked winner device (`device=winner`); no row means skip or refuse, as the
    post-lock owner check does today. They then lock only rows of an owner they hold.
  - Module type apply: lock the target device, re-read the module scoped to that device (refuse a
    moved module), scope the interface lock query to that device, order by pk, and derive template
    targets from the re-read state. No Module lock after the Device lock (NetBox 4.7 order is Module
    first).
  - Module adoption (`modules.py:483`): add `.order_by("pk")`; no device lock (2.2).
  - Cable remote-create keeps its explicit exception (round 2: an existing row aborts it).
  - **Invariant:** every transaction that locks interface rows of an owner either holds that
    owner's lock (so it serializes with the sync) or takes them in one ascending statement while it
    holds no other interface row lock.
- **Mechanism B (follow-up):** the inventory guard over every locking primitive (2.3), and lock
  orders that do not touch interface rows. r3-1's per-row-lock standalone claim is withdrawn.
- **File budget.** r4 touches three files not yet in #180's diff (`views/sync/modules.py`,
  `views/sync/migrate.py`, `views/imports/actions.py`): 100/100. Tests go to files already in the
  diff.

### Round 4 (codex `gpt-6-astra` high, read-only): A NOT RATIFIED, B NOT SEPARABLE

3.2 CLOSED. 1.1 and 3.1 NOT CLOSED. The reviewer classified every production interface-locking
site (52 `select_for_update`, 12 `relock_scoped_row`, plus implicit NetBox writes) and explored all
interleavings of each pair against the r4 sync.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 4.1 | BLOCKER | module adoption locks interface `I`, then NetBox's component counter `UPDATE`s the Device: `I -> D` against the sync's `D -> I`; one interface is enough, ordering cannot fix it | accepted (4.4.0 and 4.7.0 counter receivers) | mechanism B |
| 4.2 | MAJOR | repeated or unordered interface UPDATE/DELETE in the same outer transaction: template adoption fallback, install loops, VC normalization, replacement cascades, interface delete (+ owner counters) | accepted | mechanism B |
| 4.3 | MAJOR | NetBox 4.7's channel-rename `on_commit` callback opens a new owner-free transaction and saves children in name order | accepted; upstream behaviour | mechanism B |
| 4.4 | MAJOR | cable replacement retraces paths and updates path-origin interfaces beyond the locked owner set | accepted; upstream behaviour | mechanism B |

Also: r4-1 locks every interface of a VC for a one-row sync, and the initial owner set is
permission-restricted while the relationship scope locks all VC members.

**Clustering.** 1.1, 2.1-2.3, 3.1, 4.1-4.4 are one mechanism: **there is no global acquisition order
for interface rows, and NetBox itself takes interface and owner locks in orders the plugin does not
control** (counters, on-commit renames, path retrace). Most listed cycles already exist against
today's relationship pass (owner, then a pk-ordered interface batch) and today's attribute
`UPDATE`. An ordering rule cannot be closed at plugin level. Four rounds patched variants of it.

### r5

**Operator decision (2026-09-24): split.** The r5 core goes to one counted verdict round; a blocker
there reports the design blocked. Target: PR #180 (98/100 files).

**General fix: the attribute writer never waits on an interface row lock.** A cycle needs every
member to wait. A transaction that never waits for a lock adds no edge to any cycle.

- **r5-1.** The writer opens its own savepoint and takes the row with
  `select_for_update(of=("self",), nowait=True)`. `LockNotAvailable` (SQLSTATE `55P03`, caught
  narrowly) refuses that row: "NetBox interface <name> is being changed by another operation. Try
  again." The bulk sync reports it like any other row refusal; the IP-driven create raises the
  `ValueError` its callers already handle.
- **r5-2.** It plans and writes the fresh locked instance (r2-2). When nothing changes (fields,
  custom fields, MAC), it rolls its savepoint back with `transaction.set_rollback(True)`.
  PostgreSQL releases row locks taken inside a rolled-back savepoint. The rows the writer holds at the
  end are then exactly the rows today's `UPDATE` holds: changed rows only.
- **r5-3.** r4-1 (whole-scope prelock) and r4-2 (counterparty changes) are withdrawn. Module type
  apply keeps today's lock order and only routes through `type_change_refusal` (r2-3, r3-2). 3.2 is
  then moot: the core adds no device lock there.
- **Standalone check.** Against today, the writer drops wait edges (today's `save()` waits on a
  locked row; r5 refuses it) and holds no extra rows. Every remaining cycle needs a wait the core
  does not add. Those cycles are today's.
- **Mechanism B (follow-up issue):** deadlock exposure of the relationship pass, module
  install/adoption/replace, interface delete, cable replacement, and NetBox's own owner-free writes.
  Candidate direction: the same no-wait rule for every plugin interface-row acquisition, plus the
  inventory guard (2.3). Evidence: 1.1, 2.1-2.3, 3.1, 4.1-4.4.

### Core (split scope, for the verdict round)

1. **Check.** `interface_diff.type_change_refusal(interface, new_type) -> str | None` on a
   `copy.copy` with the new type: `clean_fields(exclude=<all but type>)`; then "has LAG members
   (`Interface.objects.filter(lag_id=pk).exists()`) and new type is not `lag`"; then the guarded
   `clean()`. First failure is the reason (with the rule names). No two-copy comparison. An
   already-invalid interface holds the type change (D7).
2. **Planner.** `planned_interface_type(interface, decision, *, created) -> PlannedType(value,
   kept_reason)`: UNMAPPED, VMInterface, `created=True`, or unchanged type -> today's behaviour, no
   query; otherwise `type_change_refusal` decides.
3. **Guard.** `utils.netbox_interface_clean(interface)` shared with `_validate_relationship`: the
   4.4.0 `virtual_chassis` fault reruns without the parent on the same chassis and raises NetBox's
   intended `ValidationError` on a different one (D3); any other `AttributeError` propagates.
4. **Writer.** `update_interface_from_port(..., created)`: asserts an atomic block; own savepoint;
   `select_for_update(of=("self",), nowait=True)` reselect (r5-1); decide the rule with the locked
   owner platform; plan; write; full save of the fresh instance; returns
   `InterfaceWriteResult(interface, changed)`; nothing changed -> `set_rollback(True)` (r5-2);
   `55P03` -> row refused "being changed by another operation". Callers pass `created` from their
   `get_or_create` (`False` on id/name branches) and continue with the returned instance.
5. **Diff.** `compute_row_sync_state` plans once per existing row; `_type_differs` compares `value`;
   `RowSyncState.type_kept_reason` renders as a Type-cell note; a kept type is not a difference.
   Table, verify repaint and single-row response share it.
6. **Module type apply** calls `type_change_refusal` (refusal -> `validation_failed` with the
   reason); lock order unchanged.
7. **AST guard (D9):** `Interface.type` is assigned only by the writer via the planner, module apply
   via the check, the ratified promotions and their restore callbacks, and creates.
8. **Tests:** AC1-AC7 as in 6A with r2-5 (real 4.4.0 on CI's leg), D10, the 1.4/2.5 creation cases,
   a NOWAIT refusal test (a second connection holds the row lock; the sync refuses that row and
   commits the others), and a savepoint-release test (an unchanged row's lock is not held after the
   writer returns: a second connection can take it with NOWAIT).

**Deferred (follow-up issue, filed after the core is implemented):** plugin-wide interface-row
deadlock exposure (mechanism B), evidence 1.1, 2.1-2.3, 3.1, 4.1-4.4.

### Round 5 = core verdict round (codex `gpt-6-astra` high, read-only): r5 core NOT RATIFIED — DESIGN BLOCKED

Checked and holding: the savepoint mechanism (PostgreSQL releases row locks taken after a savepoint
on rollback to it; Django's nested `set_rollback(True)` gives `SAVEPOINT -> ROLLBACK TO -> RELEASE`
with the outer transaction usable); planner and module apply take no row locks; NOWAIT refusal
leaves no partial state; skeleton and IP paths; no CLOSED finding reopened.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 5c.1 | BLOCKER | while it holds the NOWAIT-locked interface, the writer still waits on other rows: `assign_interface_mac` calls `mac_addresses.add()` unconditionally, which `UPDATE`s the MAC row even when nothing changes; `BaseInterface.save()` clears tagged-VLAN relations before the row save. Today an unchanged row takes no interface lock, so r5 adds `I -> M` wait edges | confirmed: `interface_sync.py:79` (unconditional `add`) | open |
| 5c.2 | MAJOR | after an unchanged writer releases its lock, the VLAN helper full-saves the returned instance and overwrites a concurrent `lag_id` (AC5 unmet) | confirmed: `views/sync/interfaces.py:1585` -> `views/mixins.py:1712` full `save()` | open |

**Clustering.** Both come from the same place: the writer's lock boundary is smaller than the row
operation. The row operation is the attribute writer, the MAC assignment and the VLAN sync, plus
NetBox's implicit writes inside `save()`. Any early lock on `I` changes what is held while those
run.

**Status: BLOCKED** under the agreed rule (a blocker in the split core's verdict round). The
operator decides the next step.

**Candidate r6 (not reviewed): late lock.** No lock until the row has a change. Plan on a fresh
unlocked read, then run the MAC step (MAC before `I`, as today). If any field changes, take
`select_for_update` on `I` at the point where today's `UPDATE` already takes and waits for that row
lock, re-read, re-plan the type against the locked state, then `save(update_fields=<changed>)`. The
VLAN helper saves with `update_fields` too. The wait edges and held rows are then the same as today.
The redundant MAC `add()` becomes a no-op when the MAC is already attached. Known open point: the
members rule reads other rows (`lag_id` on members), which a lock on `I` does not stabilise under
any scheme so far.

**Operator decision (2026-09-24): rescope to the check only.** No new interface row lock in #185.
The row lock, the plan-vs-concurrent-link race, and plugin-wide deadlock exposure go to one
follow-up issue (evidence 1.1, 2.1-2.3, 3.1, 4.1-4.4, 5c.1, 5c.2, and the members-rule note above).
The rescoped core gets one verdict round.

### Rescoped core (check only, for its verdict round)

Kept unchanged from "Core (split scope)": 1 (check), 2 (planner with `created`), 3 (guard),
5 (diff), 6 (module type apply), 7 (AST guard). Changed:

- **Writer.** No new lock and no reselect: it plans against the instance its caller's resolver
  just read in the same transaction. Callers pass `created` from `get_or_create` (`False` on id/name
  branches). The save becomes `save(update_fields=<tracked fields that changed> + custom_field_data
  when it changed>)`, so a stale instance never writes `lag`, `parent`, `bridge` or any other column
  the writer does not own. It returns the bool it returns today. r2-2, r5-1, r5-2 are withdrawn.
- **VLAN helper** (`views/mixins.py:1712`): `save(update_fields=["mode", "untagged_vlan"])` for the
  same reason (5c.2).
- **MAC** (`interface_sync.py:79`): skip `mac_addresses.add()` when the MAC is already attached
  (a redundant `UPDATE`, found in 5c.1).
- **AC5 reworded:** a concurrent link write between the table load and the sync is not lost. The
  planner reads the link state current at planning time. A link written by another transaction
  between planning and the `UPDATE` is a known race, deferred with the row lock.
- **Tests:** as Core 8, minus the NOWAIT and savepoint tests; plus a lost-update test for the writer
  and for the VLAN helper (stale instance, concurrent `lag_id` in the DB, sync, `lag_id` survives;
  mutation: full `save()` goes red).
- **File budget:** `views/mixins.py` is new to #180's diff: 98/100.

### Rescoped core verdict round (codex `gpt-6-astra` high, read-only): NOT RATIFIED on the partial saves only

| # | sev | finding | status |
|---|---|---|---|
| 6.1 | MAJOR | `update_fields` without `_name` leaves NetBox's natural-ordering key stale after a rename (`NaturalOrderingField.pre_save` runs only for saved fields) | withdrawn with the partial saves |
| 6.2 | MAJOR | `update_fields` without `last_updated` stops advancing the `auto_now` timestamp | withdrawn with the partial saves |

No finding on items 1-3 and 5-7. The reviewer executed 4.4.0's real `Interface.clean()` with the
guard (same chassis accepted; different chassis, physical child and virtual-with-LAG refused),
checked the MAC no-op (identical primary assignment and change result in all three cases), and found
no stale-object defect from dropping the reselect.

**Operator decision (2026-09-24): defer the lost update.** The writer and the VLAN helper keep
today's full `save()`. The stale-save lost update (5.3, 5c.2) moves to the concurrency follow-up
with the row lock. **Closed on the reviewer's own executed evidence at the cap**: the rescoped core
minus the partial saves has no open finding.

## 7. Ratified scope for issue #185 (2026-09-24)

1. `interface_diff.type_change_refusal(interface, new_type) -> TypeRefusal | None`: on `copy.copy`
   with the new type, `clean_fields(exclude=<all but type>)`, then the members rule
   (`Interface.objects.filter(lag_id=pk).exists()` and the new type is not `lag`), then the guarded
   `clean()`. The first failure is the reason. No two-copy comparison. `TypeRefusal` holds the
   message and the `Interface` field it refuses (None for any other key), and marks the members
   rule as the plugin's own text.
2. `interface_diff.planned_interface_type(interface, decision, *, created) -> PlannedType(value,
   kept)`. UNMAPPED, VMInterface, `created=True` or an unchanged type: today's behaviour, no
   query. Otherwise `type_change_refusal` decides; a refusal keeps the current type, and `kept`
   holds the rule names, the planned type and the refusal.
3. `utils.netbox_interface_clean(interface)`, shared with `_validate_relationship`: the 4.4.0
   `virtual_chassis` fault reruns without the parent on the same chassis, and raises NetBox's
   intended `ValidationError` on a different one. Any other `AttributeError` propagates.
4. Writer: `update_interface_from_port(..., created)` sets the planned type and logs the kept note,
   with NetBox's full message, at warning. Callers pass `created` from `get_or_create` (`False` on
   the id and name branches). Save, lock and return value are unchanged.
5. Diff: `compute_row_sync_state` plans once per existing row; `_type_differs` compares `value`;
   `RowSyncState.type_kept` renders as a Type-cell note for the table's user; a kept type is not a
   difference. Table, verify repaint and single-row response share it.
6. Module type apply calls `type_change_refusal`; a refusal returns `validation_failed` with the
   refusal, and the warning shows it through the rule in item 9. Lock order unchanged.
7. AST guard: `Interface.type` is assigned only by the writer via the planner, module apply via the
   check, the ratified promotions and their restore callbacks, and creates. The guard also finds ORM
   writes: `update(type=...)`, `bulk_update` with `type`, and `type` in `*_or_create` defaults.
8. `assign_interface_mac` skips `mac_addresses.add()` when the MAC is already attached.
9. Disclosure: only an authenticated, active superuser gets NetBox's refusal text, in the Type
   cell, the verify repaint and the module apply warning (`TypeRefusal.text_for`). Every other
   viewer gets the rule, the planned type and "NetBox refuses the <field> field", where <field> is
   the name of a concrete `Interface` field that the error key resolves to. Any other key reads as
   "NetBox refuses the interface", because a key can be any text. The members rule text names no
   object and shows to every viewer, and the writer log keeps the full text. A map from field to
   named objects was dropped, because admin `CUSTOM_VALIDATORS` and other plugins' `post_clean`
   receivers can put any text under any field, so no map can prove a message safe.

**Acceptance conditions:** AC1, AC2, AC3, AC4, AC6 (as in D7), AC7 (real 4.4.0 `clean()` on CI's
`v4.4.0` leg; mutation: remove the guard -> red). Plus: the D7 case (an unrelated `clean()` error
holds the type), the 1.4/2.5 creation cases (a skeleton created with a `channel` mapping writes it;
a pre-existing type-less row is checked), module apply refusing a non-`lag` template type on an
aggregate with members, and a query-count test (no extra query for a row whose type matches). AC5
is deferred.

**Follow-up issue (file after this scope is implemented):** interface-row concurrency. It covers the
row lock, the plan-vs-concurrent-link race, the stale full-save lost update (5.3, 5c.2), and
plugin-wide deadlock exposure. Evidence: 1.1, 2.1-2.3, 3.1, 4.1-4.4, 5c.1, 5c.2, 6.1, 6.2, and the
members-rule note. Candidate directions on record: late lock (r6), no-wait acquisition (r5).

## 8. Follow-up: interface-row concurrency (issue #188)

Status: **RATIFIED (Core r5, split scope), 2026-09-24.** See 8.9 for the scope and the next action. Target branch: `feat/interface-member-badges` (PR #180, 97/100
changed files). The record stays in this file to save a file slot.

### 8.1 Brief

**Decision.**

- (A) How a plugin write of an existing interface row stops overwriting a concurrent change to a
  column the plugin does not write (lost update).
- (B) How the type that the sync writes is checked against the row's own links as they are when the
  row is written, not as they were when the row was read.
- (C) How a transaction that PostgreSQL aborts for a lock conflict is retried once, and then shown
  to the user as a "try again" result, never as a 500. This must use one boundary that a view uses
  now and a background job uses later.

**Problem as a class.**

- A and B come from one gap. The sync reads the interface row in its transaction, computes the
  change, and then saves the whole row. Plugin syncs of one owner queue behind the owner lock.
  NetBox's own edit paths do not take that lock: the UI edit saves in `atomic` with no row lock, and
  the API locks the row only with `If-Match`. So a NetBox edit that commits between the read and
  the `UPDATE` is overwritten.
- C: no plugin code handles SQLSTATE `40P01` (deadlock) or `55P03` (lock not available). NetBox
  renders them as a 500 page. The interface sync form's htmx error path shows no message at all.
  Section 6 round 4 shows that the plugin cannot remove deadlocks: NetBox takes interface and owner
  locks in orders the plugin does not control.

**Operator decisions in force (not open).**

1. C: deadlocks are accepted as unavoidable. The goal is safe and visible: retry once, then a "try
   again" result. A plugin-wide lock-order inventory or lock-order guard is out of scope.
2. The retry boundary must be one that a background job can use. Issue #144 plans a JobRunner
   adapter that runs reconciliation in "transaction groups". Build no part of #144 now, but do not
   build a boundary that only a view can use.
3. The LAG-members race is accepted as a known race and is out of scope. A member can join an
   aggregate in another transaction while the sync changes the aggregate's type, because the
   members rule reads other rows.
4. The semantics of the #185 check are ratified (section 7) and are not open.
5. A partial save must keep `_name` (natural ordering) and `last_updated` (`auto_now`) correct
   (findings 6.1 and 6.2). A full save of a fresh instance meets this.
6. No new branch. The work lands on #180.

**Constraints.**

- NetBox `min_version` is 4.4.0; the dev and CI container runs 4.7.0. PostgreSQL uses its default
  isolation, READ COMMITTED. NetBox does not set `ATOMIC_REQUESTS`.
- Do not make deadlocks more likely than today. A new row lock may be taken only where today's code
  already waits for that row, in the same or a weaker lock mode (see 5c.1: an early lock added
  `I -> MAC` wait edges). A plain `UPDATE` of non-key columns takes `FOR NO KEY UPDATE`.
- A row whose values do not change takes no row lock and writes nothing (today's behaviour).
- Fail closed. Catch only named SQLSTATEs. No broad exception swallowing.
- A retry re-runs a whole outermost transaction. It never re-runs a savepoint inside an aborted or
  still-open outer transaction.
- A retried unit of work must be safe to run again. Today some views add `messages`, counters and
  per-row result lists inside the transaction. Messages in session storage survive a rollback, so a
  retry would duplicate them.
- File budget: 3 free slots on #180. Tests go into files already in the diff where possible.
- Tests need a real second database connection. `transaction=True` flushes seeded rows (a known
  harness trap), and `tests/test_sync_interface_concurrency.py` already uses a second connection and
  `lock_timeout`.

**Observable acceptance conditions.**

1. Another transaction commits a change to the interface's `lag`, `parent`, `bridge`, `vrf` or
   `mark_connected`, or to any other column the writer does not write. The commit lands after the
   sync read the row and before the sync writes it. The change survives both the attribute writer
   and the VLAN helper.
2. The type the writer stores is checked (section 7 check) against the row's own `lag`, `parent`,
   `bridge` and cable as they are at the write. For example, a `lag` set concurrently on the row
   keeps a planned `virtual` type from being stored.
3. A row with no change takes no row lock and makes no write.
4. A POST to the interface sync whose transaction PostgreSQL aborts with `40P01` or `55P03` is
   retried once. If the retry also fails, the user gets a visible "try again" result for both the
   htmx and the plain submit. The database is unchanged and no message is duplicated.
5. The same `40P01` or `55P03` in any other plugin view gives the same visible result, not a 500.
   Whether those views also retry now is open decision O2.
6. Any other database error still propagates.
7. `_name`, `last_updated` and the change log stay correct for every write.
8. A job can call the same retry boundary without a request: no `request`, no `messages`.

**Open decisions.**

- O1. Where and how the late write takes the fresh row. Options include a lock at the write, an
  optimistic check, or a reselect and re-plan. Also what the writer returns to its callers (the MAC
  step and the VLAN helper run on the same instance).
- O2. Which views retry now: only the interface sync transaction groups, or every locking view (31
  view classes).
- O3. Where lock conflicts become "try again": plugin middleware, a view mixin, or a helper at each
  view. Also how the htmx client shows it.
- O4. What a lock conflict does inside a row savepoint that a broad `except Exception` wraps. It
  must reach the retry boundary, not become a row failure. Also which mechanical guard enforces that.
- O5. Whether a NetBox `AbortRequest` that NetBox raised for its own `40P01` is treated as a lock
  conflict.

**Evidence** (HEAD `e84d569a05`).

- Full saves of existing rows with no row lock: `interface_sync.py:199` (`update_interface_from_port`)
  and `views/mixins.py:1711` (`_update_interface_vlan_assignment`). They are on the same in-memory
  instance, in one transaction. An AST scan of the package (tests and migrations excluded) found no
  other unlocked full save of an existing `Interface` or `VMInterface`. The relationship pass saves
  with `update_fields` on rows locked by `build_interface_index(lock=True)`
  (`interface_relationships.py:159`). Rebind and migrate lock the row first.
- Writer callers:
  - `SyncInterfacesView.sync_interface` -> `update_interface_attributes` (`views/sync/interfaces.py:1574`,
    `:1735`), inside `atomic` `:224` / `:1079`, after `_lock_sync_scope` (owner locks: VM `:1164`; VC
    `relock_scoped_row` `:1363`, then Devices `:1370`).
  - The IP tab create-missing path: `resolve_or_create_interface_from_port` (`interface_sync.py:203`)
    <- `views/sync/ip_addresses.py:579`, in a per-row savepoint `:1119` under owner locks
    (`:472-496`). The row is locked only after the write (`:615-634`).
- The instance is read by `find_interface_by_librenms_port_id` (`utils.py:3942`) or by
  `filter().first()` / `get_or_create` (`views/sync/interfaces.py:1660-1711`), in the same
  transaction, with no row lock.
- The MAC step runs before the row save: `assign_interface_mac` (`interface_sync.py:56`). The VLAN
  helper runs after the writer: `views/sync/interfaces.py:1587` -> `views/mixins.py:1748`.
- The type check and planner: `interface_diff.planned_interface_type`, `type_change_refusal`
  (section 7).
- NetBox 4.7 facts:
  - `ObjectEditView.post` runs `atomic` with no row lock (`netbox/views/generic/object_views.py:303`).
  - The API update locks the row only with `If-Match` (`netbox/api/viewsets/__init__.py:334-338`).
  - `CableTermination.save()` re-reads and full-saves the interface (`dcim/models/cables.py:703-722`).
  - `update_interface_parents` / `update_interface_bridges` do `get` then a full `save()`
    (`dcim/utils.py:208-245`).
  - `BaseInterface.save()` clears tagged VLANs before the row save when the mode is not tagged.
  - `_original_name` is set in `__init__` (`dcim/models/mixins.py:274`) and read in `save()` (`:288`).
    `refresh_from_db()` does not reset it (finding 1.2).
- NetBox's error boundary:
  - `CoreMiddleware.process_exception` (`netbox/middleware.py:100-134`) passes `OperationalError`
    through to `handler_500`.
  - `PluginConfig.middleware` is appended after NetBox's own middleware, so its `process_exception`
    runs first. It applies to every request, not only plugin URLs (`netbox/settings.py:1016`).
  - NetBox turns its own `40P01` into `AbortRequest` in `Module._save_existing`
    (`dcim/models/modules.py:702`) and into a `ValidationError` in `netbox/models/ltree.py:438`.
    The plugin saves existing Modules (`views/sync/modules.py:2207`, `:3165`) and catches neither.
  - `JobRunner.handle` does not wrap `run()` in a transaction (`netbox/jobs.py:109-137`).
- Plugin error handling today:
  - Nothing catches `OperationalError` or checks `pgcode`. `DatabaseError` is caught only in
    `views/imports/actions.py:733` and `:3932-4040`.
  - Broad `except Exception` handlers inside row savepoints turn any database error into a row
    failure: `ip_addresses.py:1265` (with `str(exc)` in the result), `cables.py:1127`, `cables.py:277`,
    `modules.py:700/711`, `device_fields.py:974, 1044, 1206`.
- Client:
  - The interface sync form (`_interface_sync_content.html:54`) puts back the off-page selections
    and hides the spinner on `htmx:responseError` (`librenms_sync.js:1967`, `:3730`), but it shows
    no message.
  - An existing server pattern is `_htmx_error_response` (`views/imports/actions.py:255`): a 200
    with `HX-Reswap: none` and a toast.
  - An existing client pattern is `librenms_import.js:1190` (responseError -> `showErrorToast`).
- Background jobs today: `ImportDevicesJob` writes only Device, VM and VC rows, in one `atomic` per
  object. No interface writes run in a job.
- Planned job consumer: issue #144 user stories 8 (cancel between safe transaction groups), 14
  (a retry reassesses current state), 17, 18 (a changed plan assumption reports stale), 21 (a no-op
  reports unchanged), 48 and 49 (infrastructure errors mark the job failed). Also #149 and #150.
- Locking views: 31 view classes in 9 files take `select_for_update` or `relock_scoped_row` in POST.
- Section 6 findings: 1.1, 2.1-2.3, 3.1, 4.1-4.4 (lock-order exposure, now accepted), 5c.1, 5c.2
  (early lock and wait edges; stale VLAN save), 6.1, 6.2 (partial saves), and the members-rule note.

### 8.2 Claude design (Opus 5.5, drafted from the brief before reading 8.3)

**A and B: late lock, re-plan, full save.** One private helper in `interface_sync.py` owns the write of
an existing interface row. Both unlocked writers use it: the attribute writer and the VLAN helper.

1. The writer computes the change on the instance it has, as today. The MAC step runs in its
   current position. No change: no lock, no write (AC3).
2. A change: reselect the row with `select_for_update(of=("self",), no_key=True)`, filtered by
   `pk` and the expected owner. `no_key=True` is `FOR NO KEY UPDATE`, the mode that today's `UPDATE`
   takes. The lock is taken where today's `UPDATE` waits. No row means that the owner changed: the
   row is refused ("changed by another operation; refresh and try again").
3. Re-plan on the locked instance: the owned field values from the port, and the type through
   `planned_interface_type` against the locked row's own `lag`, `parent`, `bridge` and cable columns
   (AC2). Apply the values, including `primary_mac_address`, and run a full `save()` on the fresh
   instance. `_name`, `last_updated` and `_original_name` are then correct (AC7).
4. The VLAN helper leaves tagged mode: it calls `tagged_vlans.clear()` itself **before** the lock,
   so the through-row locks come before the interface lock, as today. The `clear()` in NetBox's
   `save()` (`device_components.py:904-914`) then finds no rows.
5. The writer returns the fresh instance. The callers continue with it: `sync_interface`, the VLAN
   helper, and the IP tab create-missing path. A row that this transaction created
   (`created=True`) is private to the transaction and skips the lock.

**C: one runner, one recorder, one HTTP adapter.**

- `utils.run_transaction(work)`: asserts autocommit and no enclosing atomic block. It runs `work()`
  in one outermost `atomic`, under a `connection.execute_wrapper` recorder that records every
  statement error with SQLSTATE `40P01` or `55P03` and re-raises it. A lock conflict is either an
  `OperationalError` with that SQLSTATE that escapes the transaction (this includes one raised at
  COMMIT), or any exception, or even a normal return, after the recorder saw one. So a conflict
  that a broad handler in a savepoint swallowed, or that NetBox turned into `AbortRequest` or
  `ValidationError`, still counts (O4, O5: the SQLSTATE decides, not the exception type). On a
  conflict the whole attempt rolls back and runs once more. A second conflict raises
  `TransactionConflict`.
- The first thing an attempt does is register an `on_commit` marker. A failure in a later
  `on_commit` callback (NetBox 4.7 channel rename) after a real commit is never retried. It is
  reported as "saved, but follow-up work failed".
- `work` builds fresh attempt state and returns an outcome. Messages, counters and cache
  transitions are published after the runner returns. The interface sync POST's outermost block
  (`views/sync/interfaces.py:224`) becomes the work function. It holds the attribute pass and the
  relationship pass, which is one group.
- A plugin middleware (`PluginConfig.middleware`, in `views/mixins.py`) acts only for views whose
  module is in the plugin. It maps `TransactionConflict` and a raw `40P01`/`55P03`
  `OperationalError` to one "try again" result. For htmx: a 200 with `HX-Reswap: none` and a
  toast, the `_htmx_error_response` shape. For a plain submit: one message and a redirect to a
  validated same-origin page. It also runs the recorder for other plugin views. A conflict that
  such a view swallowed as a row failure adds the "try again" message. It does not retry, because
  part of that view's work may have committed.
- The JS handles the new response. It shows the toast, puts back the off-page selections, stops the
  spinner and enables the submit button.
- A later JobRunner calls `run_transaction(lambda: apply_group(intent))` for each group.
- O2: only the interface sync retries now. The other views get the visible result now, and they
  get the retry when #144 moves them.

**Guards.** The runner's runtime assert (outermost only). An AST test that the interface sync work
function calls no `messages.*` and touches no view attribute. An AST test that
`update_interface_from_port` and the VLAN helper save an existing row only through the late-write
helper.

**Files.** New to #180's diff: `views/mixins.py` and `__init__.py`, which makes 99/100.

### 8.3 Codex design (`gpt-6-astra`, effort high, read-only, blind)

Input: section 8.1 only. It could read sections 6 and 7 as evidence, and was told to treat r5 and
r6 as history. It had a fresh context, and my draft was kept out of the repository. Full text: kept
outside the repository. Summary:

- **A and B: optimistic conditional write.** Read a fresh instance plus PostgreSQL `xmin` as a
  version token, and recheck the owner (an owner change is "stale"). Plan, run the MAC step, then a
  normal full `save(force_update=True)` whose `UPDATE` gets `AND xmin::text = %s`. It adds this
  predicate through a per-instance override of Django's private `Model._do_update`. The whole
  attempt runs in a savepoint. On zero rows updated: roll back, reselect, re-plan once. A second
  mismatch is a visible "stale / try again". There is no `SELECT FOR UPDATE`, so the row lock stays
  at today's `UPDATE` point, after the VLAN cleanup. The writer returns
  `InterfaceWriteResult(interface, changed, planned_type)`. The VLAN helper uses the same seam.
- **C:** `utils.run_transaction(work)` with the same contract as 8.2 (outermost only; `40P01` and
  `55P03`; retry once; `TransactionConflict`; fresh attempt state; publish after).
  - Swallowed conflicts: an `execute_wrapper` raises a **private `BaseException` subclass**, so no
    `except Exception` can catch it. The runner and the middleware catch it explicitly.
  - Commit errors, which `execute_wrapper` does not see: broad handlers around an outermost `atomic`
    must call a shared classifier first (cables, modules, imports).
  - O5: an `AbortRequest` counts only when its cause chain holds the SQLSTATE.
  - A committed marker for `on_commit` failures.
  - The middleware lives in `views/mixins.py`, and it buffers the plugin's notices during dispatch.
  - Only the interface sync retries now.
- **Guards:** write ownership (the two writers use the seam); no bare or `BaseException` catch in
  production; broad handlers around transaction owners call the classifier; attempt purity.
- **Files:** `views/mixins.py`, `__init__.py`, `views/imports/actions.py`, which makes 100/100.
- **Its stated risks:** the private `_do_update` seam across Django versions; the coverage of the
  `BaseException` escape; post-commit work.

### 8.4 Divergence table

| # | decision | 8.2 (Claude) | 8.3 (codex) | evidence | disposition | consequence |
|---|---|---|---|---|---|---|
| E1 | how A/B takes the fresh row | late `FOR NO KEY UPDATE` reselect, re-plan, full save; explicit tagged-VLAN pre-clear | `xmin` predicate added to the real `UPDATE` through a private `_do_update` override; savepoint; re-plan once, then stale | `device_components.py:904-914`: `save()` clears tagged VLANs before the `UPDATE`, so a plain late lock reverses T -> I when the VLAN helper leaves tagged mode (codex's objection holds); the pre-clear restores T -> I. `_do_update` is private Django API; codex names it its top risk | **8.2 with the pre-clear.** Public API only; no stale outcome or savepoint loop. The pre-clear closes codex's wait-edge objection. | test: VLAN helper leaves tagged mode while a second connection holds the through rows; statement order T then I (SQL capture) |
| E2 | a conflict swallowed inside a savepoint | recorder: record the SQLSTATE, re-raise, and decide at the runner (or in the middleware for other views) | a private `BaseException` escapes every `except Exception` | Django's handler (`convert_exception_to_response`) catches `Exception` only, so a `BaseException` that leaks out of the adapter ends the request outside Django's handling; it also changes the row-failure semantics of every other view | **8.2.** For the retry path the outcome is the same (the group rolls back and retries). Other views keep their row semantics and add a visible message. | test differs: an IP-tab row savepoint that swallows a `40P01` -> 8.2 commits the other rows and adds the message; 8.3 rolls back the whole POST |
| E3 | commit-time conflicts in other views | the runner sees the outermost commit; for other views, a swallowed commit error stays a row failure | a classifier call in every broad handler that wraps a transaction owner (+ `actions.py`, 100/100) | `execute_wrapper` does not wrap `commit()` | **8.2, contested.** A row failure is safe and visible; the brief's AC5 targets the 500. Round 1 to judge. | the file budget differs by one |
| E4 | O5 `AbortRequest` | the recorder saw the SQLSTATE -> conflict | cause chain classification | `dcim/models/modules.py:702` raises `AbortRequest` from the `OperationalError`; the statement error passes the recorder first | **8.2** (subsumes it). | none |
| E5 | post-commit failure | not in the first draft | committed marker | NetBox 4.7 `dcim/models/mixins.py:300-320`: rename cascade in `on_commit`; Django 6.1 `run_and_clear_commit_hooks` raises out of `atomic` exit (non-robust) | **8.3 adopted** (now in 8.2). | test: a failing `on_commit` after commit is not retried |
| E6 | the owner changed under the write | the reselect filters by owner; no row -> refused | owner recheck -> stale | both | **same.** | |
| E7 | guards | runtime assert; attempt-purity AST; two-writer seam AST | + no `BaseException` catch; + classifier on transaction-owner handlers | follows E2, E3 | **8.2.** | |
| E8 | middleware notice buffering | the work function publishes after the runner; no buffering | the middleware buffers the plugin's notices during dispatch | only the interface sync retries | **8.2.** Buffering is only needed if a retried view adds messages inside the attempt, and the attempt-purity guard forbids that. | |

Both designs agree on: `run_transaction` in `utils.py` (outermost only, `40P01`/`55P03`, retry
once, typed failure, fresh attempt state, publish after); the interface sync POST block as the
first group; only the interface sync retries now; a plugin-scoped middleware in `views/mixins.py`
with the `HX-Reswap: none` toast; the writer returns the fresh instance; created rows skip the
check; and the JobRunner calls the same runner.

### r1 (merged candidate)

8.2 as written, with E5 folded in. Open for round 1: E1 (the wait edges of the late lock with the
pre-clear), E2, E3.

### Round 1 (codex `gpt-6-astra` high, read-only, executed in a scratch database): r1 NOT RATIFIED

Execution: Django 6.1, NetBox 4.7.0, PostgreSQL 18.4, a scratch database created and dropped by the
reviewer. E2 held: the recorder sees swallowed statement errors, errors that NetBox translates,
signal queries, M2M `clear()` and `on_commit` queries. The runner must reject a recorded conflict
**inside** the outer `atomic` block, before a normal exit (executed: a swallowed `55P03` otherwise
commits). The E5 marker holds (FIFO callbacks; the marker takes precedence over the recorder).
`no_key=True` produces `FOR NO KEY UPDATE OF`.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 1.1 | BLOCKER | NetBox clears tagged VLANs before **every** save of a non-tagged interface, so any lock before `save()` puts `I` before `T` for the attribute writer too; even with the pre-clear, an association inserted after the pre-clear gives `T -> I -> T` (executed: `40P01` under r1, clean under today's order) | confirmed: `device_components.py:904-914` (`clear()` whenever `mode != tagged` on an existing row) | accepted, r2-1 |
| 1.2 | MAJOR | a commit-time conflict (deferred FK) bypasses `execute_wrapper`; a broad handler (`cables.py:1127`) swallows it as a row failure, so AC5 is unmet (executed: the recorder held 0 conflicts) | confirmed; an AST scan finds 9 broad handlers that lexically wrap an `atomic` block, in 7 files (not outermost-only; helper-owned transactions not seen) | accepted, r2-2 |
| 1.3 | MAJOR | a fresh instance has no `_prechange_snapshot`, so the change log loses the before-state | confirmed, and pre-existing: the writer never calls `snapshot()`; only Rebind (`views/sync/interfaces.py:1958`) and the VLAN sync do | accepted, r2-3 |
| 1.4 | MAJOR | a rolled-back attempt leaves its events in NetBox's request `events_queue` (a `ContextVar` dict mutated in place, `core/signals.py:153`); the retry can dispatch an event for a rolled-back object | confirmed: `netbox/context.py:11`, `netbox/context_managers.py:19-33` | accepted, r2-4 |

### r2 (changes from r1, verbatim)

- **r2-1. The lock is taken at today's `UPDATE` point, by a `pre_save` receiver, with a version
  check.** The E1 disposition is reversed: codex's optimistic check is adopted, and the seam moves
  from the private `_do_update` to the public `pre_save` signal.
  - The late-write helper starts each attempt with one query. It reads a fresh instance with its
    row version (`annotate(RawSQL("xmin::text"))`), filtered by `pk` and the expected owner. No row
    means that the row is refused (owner changed).
  - It plans on the fresh instance, runs the MAC step in its current position, and sets the owned
    fields. No change: it returns, with no lock and no write (AC3).
  - A change: it marks the instance with the expected version and calls a normal full `save()`.
    NetBox's `BaseInterface.save()` clears tagged VLANs. Then Django sends `pre_save`, and a plugin
    receiver runs for a marked instance only. It runs
    `SELECT xmin::text ... WHERE id = %s FOR NO KEY UPDATE` (or `FOR UPDATE` when the save changes
    `name`, which is in a unique constraint and so makes today's `UPDATE` take `FOR UPDATE`). It
    compares the result with the mark. The lock is on the same row and at the same point as today's
    `UPDATE` wait, after the clear. NetBox 4.7 has no `pre_save` receiver on `Interface`
    (`dcim/signals.py:77` is for scope models).
  - A version mismatch raises a private exception out of `save()`. The attempt's savepoint rolls
    back (with its MAC and VLAN work, and its events, r2-4). The helper then re-reads, re-plans and
    tries once more. A second mismatch refuses the row: "changed by another operation; refresh and
    try again".
  - `xmin` also catches NetBox's queryset `.update()` writes, which do not move `last_updated`.
  - Once the transaction has updated the row, it holds the lock until commit, so a later writer in
    the same transaction (the VLAN helper after the attribute writer) reads its own version and
    always matches.
  - No explicit pre-clear. The VLAN helper keeps its current order: scalar save, then M2M. A change
    to tagged associations only still takes no row lock.
  - A created row (`created=True`) skips the version check.
- **r2-2. Commit-time conflicts become statement errors or are noted.**
  - `run_transaction` calls `connection.check_constraints()` as the last step inside the outer
    block. On PostgreSQL this runs `SET CONSTRAINTS ALL IMMEDIATE`, so a deferred check fails as a
    statement that the recorder sees. Then the runner rejects a recorded conflict before exit
    (round 1's condition).
  - Other views: each broad handler whose `try` body holds an `atomic` block calls
    `note_lock_conflict(exc)` first. That call records the SQLSTATE from the exception chain for the
    middleware and does not change the handler's row semantics. The middleware then adds the one
    "try again" message.
  - Guard: an AST test finds every broad handler (`Exception`, `DatabaseError`, `OperationalError`,
    bare) whose `try` body lexically contains an `atomic` block. Each must call
    `note_lock_conflict`, and the test has negative fixtures. Limit: it does not see a transaction
    owned by a called helper. New file: `views/imports/actions.py` (100/100).
- **r2-3. Change-log snapshot.** The late-write helper calls `snapshot()` on the fresh instance
  before it sets any field.
- **r2-4. One attempt context.** `attempt_scope()` wraps an `atomic` block, saves a deep copy of
  NetBox's `events_queue` on entry, and restores it when the block exits with an exception. The
  runner uses it for the outermost attempt, and the late-write helper uses it for its savepoint
  attempt. If `netbox.context.events_queue` is missing on NetBox 4.4.0, that is a finding.
- **Files:** `views/mixins.py`, `__init__.py` and `views/imports/actions.py` are new to #180's
  diff: 100/100.

**Operator decision (2026-09-24), file budget:** if the design needs more files than #180 has free,
the work goes to a new PR stacked on #180. Place code by module boundaries, not to save file slots.

### Round 2 (codex `gpt-6-astra` high, read-only, executed in a scratch database): r2 NOT RATIFIED

1.1 CLOSED (the `pre_save` lock follows NetBox's clear in all 25 mode transitions, both models).
1.3 CLOSED (real `snapshot()` then `to_objectchange()` gives correct before and after state). 1.2
and 1.4 NOT CLOSED. Also checked and holding:
- Raising from the receiver rolls back the VLAN clear and the new MAC. It leaves no commit
  callbacks, sends no `post_save`, and leaves `_original_name` unchanged. The failed instance must
  be discarded.
- `xmin`: a locking read after a concurrent update returns the new token. HOT updates change it.
  Writes in one transaction share it. `VACUUM FREEZE` keeps it.
- `check_constraints()` runs `SET CONSTRAINTS ALL IMMEDIATE`, and the recorder then sees a
  deferred-FK `55P03`. A missing FK stays `IntegrityError`.
- NetBox 4.4.0 has the same `events_queue` `ContextVar` dict. Its entries are plain dicts, and in
  4.7 they are `EventContext` objects.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 2.1 | BLOCKER | `primary_mac_address` is a `OneToOneField` (unique index), so a MAC change makes today's `UPDATE` take `FOR UPDATE`; r2 holds `FOR NO KEY UPDATE` and then upgrades, which deadlocks with a `FOR KEY SHARE` holder that updates the row (executed: today OK, r2 `40P01`) | confirmed: `dcim/models/device_components.py:864`; PostgreSQL's rule covers every column in a non-partial unique index usable by a FK | accepted, r3-1 |
| 2.2 | MAJOR | helper-owned transactions under broad handlers (`import_utils/vm_operations.py:147` under `:332`) escape the lexical inventory, so AC5 is unmet | confirmed; the class cannot be closed by a lexical guard | operator decision, r3-4 |
| 2.3 | BLOCKER | deep-copying `events_queue` raises `TypeError` (queued events hold the request and its stream); a shallow copy is contaminated by later in-place updates | confirmed: `extras/events.py:149` keeps the request | accepted, r3-3 |
| 2.4 | MAJOR | restoring the queue on any exception drops the events of a committed transaction when a later `on_commit` callback fails | confirmed (executed by the reviewer) | accepted, r3-3 |

**Operator decision (2026-09-24), AC5:** a view that does not use the runner must never answer a
lock conflict with a 500. A conflict that such a view catches itself keeps that view's own failure
report. No handler inventory and no request-wide recorder. A view gets the full behaviour when it
moves to the runner (#144).

### r3 (changes from r2, verbatim)

- **r3-1. The lock mode follows the changed key columns.**
  - `key_columns(model)` is derived once from `_meta`. It holds every column that PostgreSQL can
    use as a FK target: `unique=True` fields (this includes `OneToOneField`), `unique_together`,
    and `UniqueConstraint`s with plain fields and no condition or expressions.
  - The late-write helper compares every concrete column of the fresh instance with the value it
    will save. If any changed column is a key column, the receiver takes `FOR UPDATE`, otherwise
    `FOR NO KEY UPDATE`. A change to `name` always counts as a key change.
  - Drift guard: a DB test compares `key_columns()` with the unique, non-partial, non-expression
    indexes that `pg_index` reports for the `Interface` and `VMInterface` tables.
- **r3-2. A stale version is a conflict, retried by the runner.** The savepoint re-plan loop in
  the helper is removed.
  - A version mismatch in the receiver raises `ConcurrentRowChange` and records it in the
    runner's recorder. So a broad handler that swallows it cannot hide it.
  - The runner treats it like `40P01`/`55P03`: it rolls the whole attempt back and runs it once
    more. A second conflict of either kind raises `TransactionConflict`.
  - Outside the runner (the IP tab create-missing path), the exception reaches that view's row
    handling. Its message is the fixed text "NetBox interface <name> was changed by another
    operation. Refresh and try again."
- **r3-3. The event queue is scoped to the runner's attempt.** The deep copy and the restore are
  removed.
  - The runner sets `events_queue` to a new empty dict for each attempt (a `ContextVar` token),
    and resets it after the attempt's `atomic` exit. `on_commit` callbacks run inside that exit,
    so their events land in the attempt dict.
  - If the committed marker is set, the attempt's entries are appended to the request's dict. A
    key that is already there (from an earlier committed transaction in the same request) is
    stored under a derived key, so both events are kept. `flush_events` reads only the dict values
    in 4.4.0 and 4.7.0.
  - If the marker is not set (rolled back), the attempt dict is discarded. The only interface to
    the entries is "an opaque dict value", so the code does not depend on the version.
- **r3-4. AC5 as decided.** The middleware maps a conflict that escapes a plugin view to the "try
  again" result: `TransactionConflict`, or an `OperationalError` with `40P01`/`55P03` anywhere in
  the exception chain (so NetBox's `AbortRequest` raised from its own `40P01` counts, O5). There
  is no recorder outside the runner and no `note_lock_conflict`. `check_constraints()` stays as the
  runner's last step inside the outer block. `views/imports/actions.py` is not touched. AC5 now
  reads: "A `40P01`/`55P03` that escapes any other plugin view gives the same visible result, not
  a 500."
- **r3-5. Placement (operator: new PR over the cap).** New modules: `transactions.py` (the runner,
  the recorder, the conflict types, the attempt-scoped events, `key_columns`, the `pre_save`
  receiver) and `middleware.py` (the HTTP adapter). The work lands on a new PR stacked on #180.

### Round 3 (codex `gpt-6-astra` high, read-only, executed in a scratch database): r3 NOT RATIFIED

2.1 CLOSED: `_meta` key columns equal PostgreSQL's for both models. `Interface`: `id`,
`device_id`, `name`, `parent_id`, `channel_id`, `primary_mac_address_id`. `VMInterface`: `id`,
`virtual_machine_id`, `name`, `primary_mac_address_id`. The 2.1 schedule completes under r3.
2.3 CLOSED. 2.4 CLOSED for the original case. 1.2 and 2.2 MOOT (AC5 decision). 1.4 NOT CLOSED
(see 3.1).

Also checked:
- `_name`, `last_updated` and a cleared `untagged_vlan_id` are set inside `save()`, but none of
  them is a key column. So comparing the planned key values is enough.
- A later-row conflict inside the runner leaves exactly one committed result after the retry.
- The only other readers of the queue are `event_tracking` (`.values()`) and `clear_events`
  (which replaces the dict), in both 4.4.0 and 4.7.0. The runner must merge the **current**
  `ContextVar` value before it resets the token.
- Without a request, NetBox's receiver enqueues nothing.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 3.1 | MAJOR | IP create-missing path: a stale-version conflict rolls the row savepoint back but the MAC's creation event stays queued and is flushed (executed) | confirmed, and the class exists today: `_lock_target_interface` (`views/sync/ip_addresses.py:~627`) can raise after the writer created a MAC, with the same result | split: deferred |
| 3.2 | MAJOR | NetBox's channel-rename `on_commit` callback runs its own transaction; if it rolls back, its queued events are merged because the parent's marker is set (mechanism executed; full rename path inferred) | confirmed: `dcim/models/mixins.py:304-337`; the same leak exists today without the runner | split: deferred |
| 3.3 | MAJOR | "a conflict anywhere in the chain" hides a different escaping DB error (for example `23505` raised while handling a `55P03`); but NetBox's `ltree.py:438` raises its conflict `ValidationError` `from None`, so only `__context__` keeps it | confirmed | accepted, core item 9 |

**Operator decision (2026-09-24): split.** Correct events across savepoint rollbacks and across
rollbacks of `on_commit` callback transactions is a class that exists today, both in the plugin and
in NetBox. It goes to a follow-up issue (evidence 1.4, 2.3, 2.4, 3.1, 3.2). The retry itself never
leaks events, because the runner scopes them per attempt. The core below gets one counted verdict
round.

### Core (r4, split scope, for the verdict round)

1. **Runner** (`transactions.py`): `run_transaction(work)`.
   - It asserts autocommit and no enclosing atomic block.
   - Each attempt sets a fresh `events_queue` dict, opens one outermost `atomic`, and registers
     the committed marker first. It runs `work()` under the conflict recorder
     (`connection.execute_wrapper`), then calls `connection.check_constraints()` as the last step
     inside the block. A recorded conflict raises inside the block, so the attempt rolls back.
   - A conflict is any of: `OperationalError` `40P01`/`55P03` (a statement error or at COMMIT); a
     recorder hit (also for a swallowed or translated error); `ConcurrentRowChange` (item 3).
   - The first conflict re-runs the attempt; a second raises `TransactionConflict`.
   - After the attempt exits, the runner reads the current queue value. If the marker is set, it
     merges that dict into the request queue (a colliding key gets a derived key). A later
     callback failure is never retried: it raises `CommittedFollowUpError` from the original. If the
     marker is not set, it discards the dict. Then it resets the token.
   - Any other error propagates unchanged.
2. **Attempt purity.** `work` builds fresh attempt state and returns an outcome. The caller
   publishes messages, counters and cache transitions after the runner returns. The interface sync
   POST's outermost block (`views/sync/interfaces.py:224`) becomes the work function, and it holds
   the attribute pass and the relationship pass. The relationship pass's in-transaction warning
   (`:648`) becomes part of the outcome. The existing `IntegrityError` handler stays outside the
   runner call.
3. **Late write** (`interface_sync.py`, a helper shared by the attribute writer and the VLAN
   helper's scalar save).
   - One fresh read with `xmin`, filtered by `pk` and expected owner. No row means the row is
     refused.
   - `snapshot()`, then the plan (the #185 check on the fresh instance), then the MAC step in its
     current position, then the owned fields.
   - No change: return, with no lock and no write.
   - A change: mark the instance with the version and the lock mode, and run a full `save()`.
     The lock mode is `FOR UPDATE` when a planned key-column value changes (`key_columns(model)`
     from `_meta`, drift-tested against `pg_index`), otherwise `FOR NO KEY UPDATE`.
   - The `pre_save` receiver locks the row with that mode after NetBox's tagged-VLAN clear and
     compares `xmin`. A mismatch records the conflict and raises `ConcurrentRowChange`, with the
     fixed text "NetBox interface <name> was changed by another operation. Refresh and try again."
   - The writer returns the fresh instance, and the callers continue with it: `sync_interface`,
     the VLAN helper (M2M after the scalar save, as today) and the IP tab create-missing path.
   - A created row (`created=True`) skips the check.
4. **HTTP adapter** (`middleware.py`, `PluginConfig.middleware`). It acts only for views whose
   module is in the plugin. It maps a conflict that escapes to one "try again" result: for htmx, a
   200 with `HX-Reswap: none` and a toast; for a plain submit, one message and a redirect to a
   validated same-origin page. The JS for the sync forms shows the toast, puts back the off-page
   selections, stops the spinner and enables the button.
5. **Classifier** (3.3), used by the middleware:
   - `CommittedFollowUpError`: never a conflict. When its cause is a conflict, the middleware
     shows "Changes were saved, but follow-up work failed. Refresh and check the result."
     Otherwise it propagates.
   - `TransactionConflict` and `ConcurrentRowChange`: a conflict.
   - A `DatabaseError`: its own SQLSTATE decides.
   - NetBox `AbortRequest` or `ValidationError`: the nearest `DatabaseError` in its chain
     (`__cause__`, else `__context__`) decides.
   - Anything else is not a conflict.
6. **Scope (O2, AC5 decision):** only the interface sync uses the runner now. Other plugin views
   get "no 500" through items 4 and 5. A conflict they catch keeps their own report.
7. **Guards:**
   - The runner's runtime assert.
   - An AST test: the interface sync work function calls no `messages.*` and sets no view
     attribute.
   - An AST test: the two writers save an existing row only through the late-write helper.
   - The `key_columns` drift test.
8. **Tests:** end to end through the interface sync POST (htmx and plain) with a real second
   connection.
   - AC1 for each unowned column and for the VLAN helper; AC2 (a concurrent `lag` blocks a planned
     `virtual`); AC3 (captured SQL, no lock).
   - AC4: a real `55P03` on attempt one, then clean; and on both attempts.
   - A real `40P01` with controlled barriers.
   - The 2.1 upgrade schedule; the 1.1 VLAN schedule; the IP-path stale row.
   - Classifier cases (`23505` after `55P03`; `ltree` `ValidationError from None`; `AbortRequest`).
   - A committed attempt with a failing callback is not retried and keeps its events.
   - A rolled-back attempt's events are discarded.
   - A mutation check for each test.
9. **Placement:** a new PR stacked on #180 (operator decision).

**Deferred to follow-up issues (filed after the core is implemented):** (a) correct events across
savepoint and callback-transaction rollbacks (1.4, 2.3, 2.4, 3.1, 3.2); (b) moving the other locking
views to the runner (#144).

### Round 4 = core verdict round (codex `gpt-6-astra` high, read-only, executed): Core r4 NOT RATIFIED — DESIGN BLOCKED

1.1, 1.3, 2.1 and 3.3 (for the item 5 classifier) are CLOSED. 1.2 and 2.2 are MOOT. 1.4, 2.3,
2.4, 3.1 and 3.2 are DEFERRED. The split is valid: the core does not make any deferred case worse
than today. Checked by execution:
- `check_constraints()` placement: a blocked deferred FK is recorded as `55P03`; a missing FK
  stays `23503`.
- A failed first attempt leaves only the successful attempt's rows and events.
- The merge takes the current queue value.
- `CommittedFollowUpError` keeps the committed events and does not retry.
- The key-lock schedule completes under r4.
- `xmin` moves under a concurrent update.
- `key_columns` equals `pg_index`.
- `ValidationError from None` is still classifiable through `__context__`.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 4.1 | BLOCKER | item 1 makes a recorder hit sufficient, so a swallowed `55P03` followed by an unrelated escaping `23505` is retried and then raised as `TransactionConflict`, which hides the `IntegrityError` from the outer handler (AC6; executed with a prototype) | confirmed: item 1 says both "a recorder hit" is a conflict and "any other error propagates unchanged", without precedence | open |
| 4.2 | MAJOR | the attempt-purity AST guard is lexical: a helper that the work function calls can add messages or set view state (live: `_prepare_vlan_lookup_maps` warnings, `views/sync/interfaces.py:1343`, `:1351`) | confirmed | open |

**Status: BLOCKED** under the agreed rule (a blocker in the core verdict round). The operator
decides the next step.

**Candidate fix (not reviewed):**
- 4.1: one classifier, shared by the runner and the middleware (item 5). The runner retries only
  when `work` returns normally and the recorder has a hit, or when the escaping exception itself
  classifies as a conflict. Any other escaping exception propagates unchanged, whatever the
  recorder holds. A mixed-error test (swallowed `55P03`, then an escaping `23505`) must show the
  `IntegrityError` reaching the outer handler.
- 4.2: the implementation moves the helper warnings into the attempt outcome. The guard becomes an
  end-to-end retry test (a restricted VLAN scope plus a first-attempt conflict) that asserts each
  message appears once, in place of a stronger AST test.

**Operator decision (2026-09-24): one more verdict round**, which overrides the one-round rule
once, on the fixed core below.

### Core r5 (changes from Core r4, verbatim; all other items unchanged)

- **Item 1, conflict precedence (4.1).** One classifier, `classify_conflict(exc)` (item 5), is
  shared by the runner and the middleware. At the end of an attempt, the runner decides in this
  order:
  1. `work` raised: the escaping exception decides alone. If `classify_conflict(exc)` is a
     conflict, retry (or raise `TransactionConflict` on the second attempt). Otherwise the
     exception propagates unchanged, whatever the recorder holds.
  2. `work` returned normally, and the recorder holds a hit (a swallowed or translated conflict),
     or `check_constraints()` raised a conflict: raise inside the block, roll back and retry.
  3. `work` returned normally with no hit: commit.
  A COMMIT-time `OperationalError` is classified by the same function.
- **Item 5.** `classify_conflict` also treats `ConcurrentRowChange` found as the escaping exception
  as a conflict. There is no recorder lookup inside the classifier.
- **Item 7, guards (4.2).** The lexical attempt-purity AST test stays as a cheap first check.
  Transitive purity is proven by behaviour: an end-to-end retry test (a restricted VLAN scope, so
  that `_prepare_vlan_lookup_maps` warns, plus a real `55P03` on the first attempt only) asserts
  that each message appears once, and that the counters and the success banner match one attempt.
  The implementation moves the `_prepare_vlan_lookup_maps` warnings (`views/sync/interfaces.py:1343`,
  `:1351`) and every other message that the work path adds into the attempt outcome.
- **Item 8, tests.** Add a mixed-error test: a swallowed `55P03` in a savepoint, then an escaping
  `23505`. The `IntegrityError` must reach the outer handler (`views/sync/interfaces.py:250`),
  with no retry. Mutation: make a recorder hit sufficient again, and the test goes red.

### Round 5 = final verdict round (codex `gpt-6-astra` high, read-only, executed): Core r5 RATIFIED

4.1 CLOSED: the reviewer re-ran the mixed-error schedule. Under r5 the same `IntegrityError`
(`23505`) reaches the outer handler after one attempt, and the r4 rule (as a mutation) reproduces
the defect. 4.2 CLOSED at design scope: the end-to-end test that each message appears once is an
acceptance condition for the implementation. 1.1, 1.3, 2.1 and 3.3 stay closed. No new findings.
Also executed:
- A swallowed conflict still retries, and two conflicts exhaust the retry.
- An unrelated `ValueError` propagates unchanged, and a deferred-FK `23503` propagates with no
  retry.
- A callback conflict after the commit gives `CommittedFollowUpError`, with no retry and the
  committed rows and events kept.

### 8.9 Ratified scope for issue #188 (2026-09-24)

**Scope:** Core r4 items 1-9 as amended by Core r5. Operator decisions: deadlocks are accepted
(retry once, then "try again"); the LAG-members race is accepted; AC5 as decided; the split; the
work goes to a new PR stacked on #180.

**First increment:** the runner (`transactions.py`), the interface sync POST as one attempt with an
outcome that it publishes after success, and the HTTP adapter (`middleware.py` plus the sync-form
JS). Its acceptance conditions:
1. A real first-attempt conflict retries once. Two conflicts give a visible result for both
   plain and htmx submits.
2. The restricted-VLAN warnings appear once. The counters and the success banner reflect only the
   successful attempt.
3. A swallowed `55P03`, then an escaping `23505`, reaches the existing handler with no retry.
4. The rows and events of a failed attempt are gone. A failed callback after a commit is never
   retried.
5. The runner works without a request and refuses an enclosing transaction.

**Second increment:** the late write (item 3). That is the `xmin` fresh read, `snapshot()`, the
`pre_save` lock with its mode from the key columns, and `ConcurrentRowChange`, plus AC1-AC3, AC7,
and the 1.1 and 2.1 lock schedules as tests.

**Follow-up issues to file after implementation:** (a) correct events across savepoint and
callback-transaction rollbacks (evidence 1.4, 2.3, 2.4, 3.1, 3.2); (b) moving the other locking
views to the runner (#144 track).

**Implementation note (increment 1, 2026-09-24): attempt purity.** Core item 7 dropped the "sets no
view attribute" half of the lexical guard. `SyncInterfacesView`'s helpers share attempt state
through `self`. `_sync_attempt` sets every attempt list and counter afresh at the start of each
attempt, and each attempt re-locks and re-reads the objects it writes. A shallow restore of the view
attributes was built and then removed: a mutation check showed that no test depended on it. A
lexical rule against view attributes would need the view's state moved into its own object, which
the #144 reconciliation module will do. Purity is proven by behaviour instead, with end-to-end
retry tests. They check exact outcome counts. They also cover a successful attribute write followed
by a conflict in the relationship pass, and check that there is exactly one effect and each message
appears once. Implementation review round 1 found no state carried between attempts: the owner lock
re-reads `self.object`, and the maps and lists are rebuilt.

**Implementation note (increment 2, 2026-09-24): the late write.** `interface_sync.write_interface_row`
takes the fresh read and the `snapshot()`, calls the writer's `apply(row)`, compares every concrete
column, and saves through `transactions.save_at_version`. The writer returns
`InterfaceWrite(interface, changed)`; the VLAN helper's result gains `interface`. The receiver reaches
the attempt's recorder through a `ContextVar` that `_run_attempt` sets; `row_changed()` records there
and does nothing outside the runner. A fresh read that finds no row of the expected owner raises the
same `ConcurrentRowChange`, so the retry resolves the port again. The fixed text names the row by the
name that the caller read, never by a name read after the permission check. The receiver pops the
mark, and `save_at_version` raises `RuntimeError` when no receiver checked the save. The VLAN helper
always reads fresh: a row that the attribute writer already wrote in the transaction carries the
transaction's own version, so it matches.

**Implementation note (increment 3, 2026-09-25): change scope and change log.**
- The fresh read uses the caller's change-restricted queryset. `write_interface_row`,
  `update_interface_from_port` and the VLAN helper take it as a required argument. A row that left
  the change scope before the fresh read is not found, so the writer raises `row_changed` with the
  checked name. The retry then gives the view's own out-of-scope skip; an IP tab row fails with the
  fixed text. A change after the fresh read moves `xmin`, so `save_at_version` refuses it.
  Residual, not closed: a constraint through a related row (for example `device__site__name`) can
  change without a move of the interface's `xmin`.
- This changes the order of Core item 3 ("`snapshot()`, then the plan"). The writer copies the fresh
  instance (`copy_before_change`: no query, every field value deep-copied, because
  `set_librenms_device_id` changes the custom field data in place). Then it runs `apply(row)`, and
  calls `snapshot()` on the copy only when a column changed. `keep_change_log_before_state` gives
  the copy's `_prechange_snapshot` to the saved row. Reason: `snapshot()` reads the tags, VDCs,
  wireless LANs and tagged VLANs of the row, and a bulk sync paid these reads for every unchanged
  row. The before-state stays the fresh-read state: `apply` changes no data that the serializer
  reads (the MAC step writes only the MAC's own row; the primary MAC and the custom field data
  change only in memory). NetBox 4.4.0 and 4.7 both keep the snapshot in `_prechange_snapshot`, and
  `to_objectchange` and the event snapshots read it there. A change of only the tagged VLANs saves
  no column, so the VLAN helper calls `snapshot()` itself before it changes the tagged VLANs.
- The relationship pass copies both rows of an edge the same way, gives each saved row its
  before-state, and adds `last_updated` to `update_fields`. The child of a parent link is saved
  once, with the link and its promoted type, so it has one change record.

**Implementation note (2026-09-25): the scope of every row that the sync writes.**
- The interface sync does what NetBox's edit views do: it saves, then checks that the saved rows
  are in the user's scope, and a violation rolls the whole transaction back. At the end of the
  attempt (`_sync_attempt`), after the attribute pass, the VLAN write and the relationship pass,
  each Interface or VMInterface row that the attempt created or changed must be in the user's
  change scope, and each created row also in the add scope. The sync writes a row that it created
  as a change, so a created row needs both scopes. The check sends one query for each model and
  action, and it reads the rows as they are after the last write: a constraint can name a column
  that a write sets (for example `description`, `mode` or `lag`).
- The rows come from the writes themselves. `interface_sync.collect_interface_writes()` sets a
  `ContextVar` for the attempt, and two receivers record into it: `post_save` of Interface and
  VMInterface (every `save()`, also a partial save and a save that NetBox makes), and
  `m2m_changed` of the tagged VLANs (a change of only the tagged VLANs saves no column; both sides
  of the relation). A write that skips these signals (`QuerySet.update()`, `bulk_update()`, raw
  SQL) is not recorded. The sync has none; NetBox's own cable-path upkeep of channel interfaces
  updates only cable columns this way.
- A row outside the scope raises `_RowsOutsideScopeError` inside the attempt. The runner rolls the
  attempt back and discards its events, and `classify_conflict` does not treat the error as a
  conflict, so there is no second attempt. `post()` adds one error that names only the refused
  rows and the actions, and says that nothing was saved. It publishes no success message, no
  counter and no cache transition. A plain submit gets the redirect to the tab; an htmx submit gets
  the tab fragment with the message. Each write path gives its rows the name that it read before
  its permission check (`name_interface_row`): the attribute pass the checked name, the
  relationship pass the names of its locked read. A refused row with no name raises
  `RuntimeError`: a write path that names nothing is a defect.
- The VLAN write keeps its `created` argument. A new row can be outside the change scope until the
  final check refuses it. A fresh read of that row through the change scope finds no row, and it
  gives a false `ConcurrentRowChange` ("try again") in place of the refusal.
- This replaced a check of each created row in a savepoint of its own, which reported a refused row
  as skipped and synced the other rows. Review found two defects in it: the relationship pass ran
  after the check, so it could set the LAG of a checked row and move the row out of the scope (the
  late write checks the scope of an existing row only before its change, too); and the event of a
  row that its savepoint rolled back stayed in the attempt's event queue, so NetBox sent it with
  the values of the refused row.
- The IP tab is not on the runner. It keeps its own check: after its writes, a created row must be
  in the add and change scopes (`interface_rows_outside_scope`, the one definition of the rule),
  and a refusal fails only that address. Its resolver runs in a savepoint, so the refused row is
  rolled back, but its events stay in the queue. That class existed before for its change-scope
  refusal; it is follow-up (a), filed as #191.
