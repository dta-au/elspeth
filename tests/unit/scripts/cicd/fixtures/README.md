# Gateway attestation regression inputs

`gateway-buildkit-slsa-v1.json` is the unmodified Buildx `.Provenance` JSON
captured from the public gateway index
`sha256:30ece8b27b978ff810278fe4084e62f9f73d37d10c926c12766aa71b04da8ca1`.
It was built from `edc844699a350a90a624089e12a5a1e75b7b2dde` by
[publisher run 37348491709, attempt 2](https://github.com/dta-au/elspeth/actions/runs/37348491709/attempts/2).
The underlying SLSA v1 provenance blobs were fetched and SHA256-verified:

- amd64: `sha256:0fcd96581dcce7d4e96a67a724e7322bf3a1a69ba7352eee5fb663cd78afc495`
- arm64: `sha256:cda866957cf7c0fe2db5a27cdbe8a7f37df6b3f97e514b30bb7578b0276212da`

`gateway-sbom-document-ids.json` projects only the per-platform SPDX document
IDs from the same image's captured Buildx `.SBOM` output. The production CLI
was also checked against the complete captured SBOM output. No credentials
are included. These fixtures demonstrate the current metadata format; they do
not establish signature verification or complete release qualification.
