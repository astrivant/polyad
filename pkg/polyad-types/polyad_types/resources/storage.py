"""
Describe explicit storage requirements without promising application recovery.
"""

from __future__ import annotations

from attrs import frozen


@frozen
class Persistence:
    """
    Mount an existing persistent claim using an explicitly selected StorageClass.

    Attributes:
        enabled (bool): Whether this workload requires persistent storage.
        storageClass (str | None): Required StorageClass name, matching the existing PVC.
        claimName (str | None): Existing namespaced PersistentVolumeClaim to mount.
        mountPath (str): Absolute storage mount path in application containers.
    """

    enabled: bool = False
    storageClass: str | None = None
    claimName: str | None = None
    mountPath: str = "/var/lib/polyad"

    def __attrs_post_init__(self) -> None:
        """
        Reject implicit storage classes and incomplete enabled persistence contracts.

        Returns:
            None: No return value.
        """
        if self.enabled and not (self.storageClass and self.storageClass.strip() and self.claimName and self.claimName.strip()):
            raise ValueError("enabled persistence requires storageClass and claimName")
        if not self.mountPath.startswith("/") or self.mountPath == "/":
            raise ValueError("persistence mountPath must be an absolute non-root path")
