"""Studies (layer 3). A study gets a point-in-time door and its configuration, nothing else."""

from finresearch.studies import thin_slice
from finresearch.studies.base import Study

STUDIES: dict[str, Study] = {s.name: s for s in (thin_slice.STUDY,)}
