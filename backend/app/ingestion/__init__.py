"""Document ingestion pipeline: parsing -> normalization -> chunking ->
embedding, orchestrated by app.ingestion.pipeline and invoked from the Celery
task in app.workers.tasks.document_processing."""
