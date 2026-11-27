# Example: Nextgen and Partisipa on one server

- `shared-1/`: the server (`host.yaml`, and `generated/` from `dk host shared-1 render`).
- `nextgen/`: a plain env in account `deploy`.
- `partisipa/`: a blue/green site in accounts `site`, `blue` and `green`, running the test
  app in `tests/fixtures/fcos/bluegreen-app`.

Both are stand-ins with made-up data. The SSH key in `host.yaml` and `.sops.yaml` is a
throwaway made for the VM. [EVIDENCE.md](EVIDENCE.md) is the run on a local VM;
[../../SHARED_HOST.md](../../SHARED_HOST.md) explains each file.
