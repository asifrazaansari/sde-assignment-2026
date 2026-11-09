# public_03_load performance report

Measured with the simulator's five simulated days and the final controller on the available local macOS host. These are measured results, not claims about the requested 4-vCPU Linux target.

| Measure | Result |
|---|---:|
| Live events ingested | 4,200,084 |
| Fetch + SQLite insert throughput | 97,136 events/second |
| End-to-end controller wall time | 233.29 seconds |
| Wall time per simulated day | 46.66 seconds |
| Peak resident memory | 612,532,224 bytes (about 584 MiB) |
| Maximum observed event-to-controller lag | 27 simulated minutes |
| SQLite database size at finish | 2.5 GB |
| Credits spent | 0 |

The event-rate figure measures time spent fetching event chunks and durably inserting events. End-to-end wall time also includes 480 simulated 15-minute advances, policy checks, and approval polling. The lag is measured from each event timestamp to simulator time at ingestion; it is not a production real-time SLA.

At 10× volume, linear projections give about 38.9 minutes for this full five-day run, about 7.8 minutes per simulated day, and roughly 25 GB of SQLite data. The one-day time projection is below the 10-minute target on this host, but it needs measurement on the 4-vCPU Linux target. The first clear limit is the single database's write and storage footprint; memory stayed below 600 MiB at this scale. Before 10×, partition ingestion by workflow, batch writes, and move the durable event store to an operated database.
