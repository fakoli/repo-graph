# Structural analysis coverage

The experimental native Tree-sitter owner stores source inventory, definitions,
scopes, imports, callable references, callsites and target relationships in the
shared SQLite index. Serial and bounded queued collection use that same owner.
This work has not completed the corpus, scale, query, agent, human or release
qualification in [ADR 0006](adr/0006-analysis-qualification.md).

## Source and fact boundaries

The inventory is explicit and source metadata is checked against guarded file
bytes. Repository ownership, content, loaded implementation, optional grammar
versions and configuration identify the generation. Fact ranges use physical
UTF-8 byte offsets and one-based lines, including CRLF and non-ASCII source.
Imports are source syntax records; importing a module does not establish a call.
Reference and call relationships retain different roles.

An unresolved site stays in the index with its reason and no exhaustive target
claim. A resolved relationship means a supported source binding in the stated
inventory and language scope. It does not establish runtime execution. Model
summaries and Jev rankings cannot create definitions, imports or call targets.

## Construct matrix

Each row applies to the inventoried Python, Go, JavaScript and TypeScript source
unless its language is named. The frozen evaluator records inventory, syntax,
binding, call and framework coverage separately for every selected construct.

| Construct | Inventory and syntax | Binding and call coverage | Framework coverage / ceiling |
|---|---|---|---|
| Direct declaration and call | Definitions, scopes and callsites retained | Unique visible ordinary callable declarations | Not evaluated; decorators, conditional declarations and unsupported types remain unknown |
| Stable value alias | Alias binding and callable reference retained | Single source alias chain; cycles and multiple assignments unresolved | No flow or points-to analysis |
| Shadowing | Physical declarations and sites retained | Lexical scope; parameter and unknown assignments cannot inherit an outer exact target | No dynamic scope or runtime values |
| Import alias, Python | Module/local import syntax retained | Unique inventoried relative `from` module and callable declaration | Absolute import environment, local import binding, wildcard binding and reexports unresolved |
| Import alias, JS/TS | Named, namespace, default and side-effect syntax retained | Unique relative module and ordinary exported callable; namespace member lookup | Package conditions, extension alternatives, default/reexports, overloads and computed exports unresolved |
| Import alias, Go | Import specification, alias and package declarations retained | Own declared module and eligible inventoried package; unique ordinary exported function | External dependency selection, replacements, workspace/vendor/MVS and active toolchain unqualified |
| Go package bindings | Same-directory source membership and binding counts indexed | Unique package values; known competing declarations withhold exact targets | Excluded/partial source, mixed package names, test/platform filenames, build/compiler directives, CGO and bodyless declarations withhold targets |
| Callable reference | Selected assignment, alias and return sites retained | Source-supported callable value binding | A reference does not prove invocation; this is not an all-identifier reference census |
| Callback parameter | Callsite retained | Unknown parameter value remains unresolved | Callback flow and framework invocation not evaluated |
| Receiver/interface dispatch | Callsite retained | Unresolved with an explicit type/points-to reason | Possible receiver targets are not enumerated |
| Missing import | Import and callsite retained when the syntax is supported | Missing target remains unresolved | No network/module installation or invented dependency target |
| Dynamic/computed invocation | Supported call syntax retained | Unsupported/computed callee remains unresolved | Reflection, generated code and runtime loading not modeled |
| UTF-8 and CRLF | Physical source ranges, text and digest retained | Same binding rules as the surrounding construct | No normalized-source offset substitution |
| Django/Odoo hooks and service contracts | Source can be inventoried | Not yet qualified in this implementation slice | Separate mandatory framework and explicit service-contract tasks remain open |

Go provenance marks local bindings `inventoried_package_only`, with active
build, runtime and MVS qualification false. A registered external source
snapshot comparison has its separate `declared_snapshot_only` evidence scope;
local directory resemblance cannot provide that registration.

## Observable exclusions and limits

File coverage retains configuration, unsupported language, excluded size and
partial parse statuses. Partial files retain syntax with withheld exact targets.
Admission, source bytes, per-file work, worker count, handoff, index size,
deadline and cancellation have explicit bounds. These settings are configuration
limits; representative capacity and recommended defaults remain unqualified.
Neither native wheel availability nor a grammar's language count is qualification.

The current index schema is `structural-v2`. Incompatible experimental indexes
fail closed and require a new output directory. A failure before publication
retains the prior generation. A directory-sync failure after replacement reports
the new published generation and `publication_uncertain` with crash durability
unconfirmed. No automatic retry should assume the prior generation survived.

## Reproduce the frozen construct check

After installing the optional analysis extra, run:

```sh
uv run python evaluations/analysis.py --suite constructs
```

The [facts report](../evaluations/results/code-understanding/facts.json) retains
the separately locked 75 selected definitions and 44 selected sites for each of
serial1 and queued2, actual implementation hashes, per-case results, physical
provenance checks and coverage failures. Four selected receiver cases have
reviewed target-enumeration expectations; conservative unresolved output can
pass the direct-binding gate while their missing alternatives remain failures
of receiver coverage. These selected cases are not population precision/recall
or complete business-path evidence.
