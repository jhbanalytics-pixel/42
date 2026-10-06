# Client onboarding contract v1

`validate_client_entitlement(document: dict) -> dict` validates a client entitlement document against the packaged `engine/configs/client_onboarding.schema.json`. The packaged schema is fixed at build time. A caller cannot replace it through the document.

The validator is local and pure after module initialization. It performs no provider, network, credential, approval, IAM or native resource call. `native_rights_verified` and `activation_ready` are always false. A later authority layer must verify every referenced resource and commercial right before activation.

The input uses the closed `client_entitlement_v1` schema. Every object rejects unknown keys. Market, language, source, credential, asset, role, comparator, query, benchmark, familiarity and coverage values remain configured data. The shared schema contains no client-specific market or source list.

Proof records contain exactly `status`, `evidence_ref`, `verified_at`, `expires_at` and `revoked_at`. Status is `unproven`, `current`, `expired` or `revoked`. The validator reads one UTC instant, rejects invalid chronology, and aggregates explicit leaves with precedence revoked, expired, unproven, current. The final current state is returned as `claimed_current`.

The schema permits one to nine fractional second digits. Comparisons preserve that precision as integer nanoseconds: whole UTC calendar seconds are converted from timedelta days and seconds, fractional digits are right-padded to nine, and the captured Python clock contributes `microsecond * 1000`. The validator does not use floating-point timestamps or rewrite document timestamps.

The traversal reads five keys under the top-level `proof` object: commercial approval, overall expiry, funding basis, permitted uses and retention policy. It also reads each proof in source rights, credential references, identity assets and search benchmark domains. It never searches recursively for arbitrary keys named `proof`.

Credential references are either null while unproven or full positive numeric Google Secret Manager version names. The credential row separately declares client and source identity. Resource spelling proves neither ownership nor permission.

`uncovered_contract_cells(required, proven)` accepts sets of lowercase `(market, feature)` tuples. It requires `proven` to be a subset of `required` and returns the Python lexicographic sort of `required - proven`. It performs no market aliasing or feature substitution.

Valid documents are hashed only after schema and semantic validation. Canonical bytes use UTF-8 with `ensure_ascii=False`, `allow_nan=False`, `sort_keys=True`, `separators=(",", ":")` and no trailing newline. Invalid documents receive a null digest and null client scope. Errors contain only a closed code and safe JSON pointer path. Unknown input keys and rejected credential values are never echoed.

Complete contracted coverage defaults false. It can be true as a document claim only when a full owned matrix is frozen, required cells are present, no unresolved feature or dependency remains, and every required proof is currently valid. This still does not establish native rights or activation.

Real client entitlement and requirements documents remain protected inputs outside Git. Clean tests use synthetic generic records and computed fixture values only.
