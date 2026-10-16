# Licensing

<!-- toc:start -->
**Table of contents**

- [Separately licensed components](#separately-licensed-components)
<!-- toc:end -->

Polyad uses [Business Source License 1.1](../LICENSE) (`BUSL-1.1`). The licensor
is Astrivant LLC. The Additional Use Grant is `None`: non-production use is
permitted, and production use requires a separate commercial license from
Astrivant LLC before conversion to the Change License.

Each version converts to GNU General Public License version 3.0 only
(`GPL-3.0-only`) on the fourth anniversary of its first publicly available
distribution under BSL 1.1. Releasing a new version does not restart an older
version's conversion period. BSL is source-available, not open source before
conversion. Previously published versions retain their original licenses;
existing releases and tags are unchanged.

The standard terms are published by [MariaDB](https://mariadb.com/bsl11/) and
[SPDX](https://spdx.org/licenses/BUSL-1.1.html). Project-specific parameters appear
at the top of `LICENSE`.

## Separately licensed components

Dependencies and components bearing separate license notices retain those terms.
Preserve their license texts and attribution when redistributing them.

The same project license accompanies all five Python distributions and the Helm
charts. Container metadata also declares `BUSL-1.1`.

Vendored Kubernetes resources and schemas keep their upstream Apache-2.0 notices:
see [provider licenses](../schemas/licenses/), [Dragonfly attribution](../charts/polyad/UPSTREAM.md),
the `*-LICENSE` files beside generated schema copies in the charts and
`polyad-schemas`, and [the Kubernetes notice](../integrations/minikube/full/chart/LICENSE.kubernetes).
Chart dependencies retain their bundled upstream licenses.
