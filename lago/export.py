from abc import ABCMeta, abstractmethod
import logging
import functools
import copy
import os
from os import path
import time
import json
import shutil

from . import log_utils, utils

LOGGER = logging.getLogger(__name__)
LogTask = functools.partial(log_utils.LogTask, logger=LOGGER)


class DiskExportManager(object):
    """
    ExportManager object is responsible on the export process of an image
    from the current Lago prefix.

    DiskExportManger is the base class of specific ExportManagers.
    Each specific ExportManger is responsible on the export process of an
    image with a specific type (e.g template, file...)

    Attributes:
        src (str): Path to the image that should be exported
        name (str): The name of the exported disk
        dst (str): The absolute path of the exported disk
        disk_type (str): The type of the image e.g template, file, empty...
        disk (dict): Disk attributes (of the disk that should be exported)
            as found in workdir/current/virt/VM-NAME
        exported_metadata(dict): A copy of the source disk metadata, this
            dict should be updated with new values during the export process.
            do_compress(bool): If true, apply compression to the exported disk.
    """

    __metaclass__ = ABCMeta

    def __init__(self, dst, disk_type, disk, do_compress):
        """
        Args:
            dst (str): The absolute path of the exported disk
            disk_type (str): The type of the image
                e.g template, file, empty...
            disk (dict): Disk attributes as found in
                workdir/current/virt/VM-NAME
            do_compress(bool): If true, apply compression to the
                exported disk.
        """
        self.src = path.expandvars(disk['path'])
        self.name = path.basename(self.src)
        self.dst = path.join(path.expandvars(dst), self.name)
        self.dist_type = disk_type
        self.disk = disk
        self.exported_metadata = copy.deepcopy(disk['metadata'])
        self.do_compress = do_compress

    @staticmethod
    def get_instance_by_type(dst, disk, do_compress, *args, **kwargs):
        """
        Args:
            dst (str): The path of the new exported disk.
                can contain env variables.
            disk (dict): Disk attributes
                (of the disk that should be exported) as
                found in workdir/current/virt/VM-NAME
            do_compress(bool): If true, apply compression to the exported disk.
        Returns
            An instance of a subclass of DiskExportManager which
            matches the disk type.
        """
        disk_type = disk['type']
        cls = HANDLERS.get(disk_type)
        if not cls:
            raise utils.LagoUserException(
                'Export is not supported for disk type {}'.format(disk_type)
            )
        return cls(dst, disk_type, disk, do_compress, *args, **kwargs)

    @abstractmethod
    def export(self):
        """
        This method will handle the export process and should
        implemented in each subclass.
        """
        raise NotImplementedError(
            'export is an abstract method that should'
            'be implemented in subclass'
        )

    def copy(self):
        """
        Copy the disk using cp in order to preserves the 'sparse'
        structure of the file
        """
        with LogTask('Copying disk'):
            utils.cp(self.src, self.dst)

    def sparse(self):
        """
        Make the exported images more compact by removing unused space.
        Please refer to 'virt-sparsify' for more info.
        """
        with LogTask('Making disk sparse'):
            # Removed unused space from the disk
            utils.sparse(self.dst, self.disk['format'])

    def calc_sha(self, checksum):
        """
        Calculate the checksum of the new exported disk, write it to
        a file, and update this managers 'exported_metadata'.

        Args:
            checksum(str): The type of the checksum
        """
        with LogTask('Calculating {}'.format(checksum)):
            with open(self.dst + '.hash', 'wt') as f:
                sha = utils.get_hash(self.dst, checksum)
                f.write(sha)
            self.exported_metadata[checksum] = sha

    def compress(self):
        """
        Compress the new exported image,
        Block size was taken from virt-builder page
        """
        if not self.do_compress:
            return
        with LogTask('Compressing disk'):
            utils.compress(self.dst, 16777216)
            os.unlink(self.dst)

    def update_lago_metadata(self):
        with LogTask('Updating Lago metadata'):
            self.exported_metadata['size'] = os.stat(self.dst).st_size
            self.exported_metadata['name'] = self.name
            self.exported_metadata['version'] = time.strftime("%Y%m%d.0")

    def write_lago_metadata(self):
        with LogTask('Writing Lago metadata'):
            with open(self.dst + '.metadata', 'wt') as f:
                json.dump(self.exported_metadata, f)


class TemplateExportManager(DiskExportManager):
    """
    TemplateExportManager is responsible exporting images of type template.

    Attributes:
        See superclass
    """

    def __init__(self, dst, disk_type, disk, do_compress, *args, **kwargs):
        super(TemplateExportManager, self).__init__(
            dst, disk_type, disk, do_compress
        )
        self.standalone = kwargs['standalone']
        self.src_qemu_info = utils.get_qemu_info(self.src, backing_chain=True)

    def rebase(self):
        """
        Change the backing-file entry of the exported disk.
        Please refer to 'qemu-img rebase' manual for more info.
        """
        if self.standalone:
            rebase_msg = 'Merging layered image with base'
        else:
            rebase_msg = 'Rebase'

        with LogTask(rebase_msg):
            if len(self.src_qemu_info) == 1:
                # Base image (doesn't have predecessors)
                return

            if self.standalone:
                # Consolidate the layers and base image
                utils.qemu_rebase(target=self.dst, backing_file="")
            else:
                if len(self.src_qemu_info) > 2:
                    raise utils.LagoUserException(
                        'Layered export is currently supported for one '
                        'layer only.  You can try to use Standalone export.'
                    )
                # Put an identifier in the metadata of the copied layer,
                # this identifier will be used later by Lago in order
                # to resolve and download the base image
                parent = self.src_qemu_info[0]['backing-filename']
                parent = './{}'.format(
                    os.path.basename(parent.split(':', 1)[1])
                )
                utils.qemu_rebase(
                    target=self.dst, backing_file=parent, safe=False
                )

    def update_lago_metadata(self):
        super(TemplateExportManager, self).update_lago_metadata()

        if self.standalone:
            self.exported_metadata['base'] = 'None'
        else:
            self.exported_metadata['base'] = os.path.basename(self.src)

    def export(self):
        """
           See DiskExportManager.export
        """
        with LogTask('Exporting disk {} to {}'.format(self.name, self.dst)):
            with utils.RollbackContext() as rollback:
                rollback.prependDefer(
                    shutil.rmtree, self.dst, ignore_errors=True
                )
                self.copy()
                self.sparse()
                self.rebase()
                self.calc_sha('sha1')
                self.update_lago_metadata()
                self.write_lago_metadata()
                self.compress()
                rollback.clear()


class FileExportManager(DiskExportManager):
    """
    FileExportManager is responsible exporting images of type file and empty.

    Attributes:
        standalone (bool): If true, create a new image which is the result of
            merging all the layers of src (the image that we want to export).
        src_qemu_info (dict): Metadata on src which was generated by qemu-img.
    """

    def __init__(self, dst, disk_type, disk, do_compress, *args, **kwargs):
        super(FileExportManager, self).__init__(
            dst, disk_type, disk, do_compress
        )

    def export(self):
        """
            See DiskExportManager.export
        """
        with LogTask('Exporting disk {} to {}'.format(self.name, self.dst)):
            with utils.RollbackContext() as rollback:
                rollback.prependDefer(
                    shutil.rmtree, self.dst, ignore_errors=True
                )
                self.copy()
                self.sparse()
                self.calc_sha('sha1')
                self.update_lago_metadata()
                self.write_lago_metadata()
                self.compress()
                rollback.clear()


HANDLERS = {
    'file': FileExportManager,
    'empty': FileExportManager,
    'template': TemplateExportManager
}
