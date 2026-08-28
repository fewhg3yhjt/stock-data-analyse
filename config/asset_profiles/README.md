# Asset Profiles

Asset profiles describe how each security type participates in datasets and
metrics. They do not define a task's actual range; task scope and execution
requests do that.

Each dataset or metric uses one of:

- `required`: missing data is a task/quality problem;
- `optional`: use when available, but do not block the task;
- `not_applicable`: do not count as missing or unhealthy.
