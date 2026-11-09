-- Allow late turn activity to correct aggregate counts without adding duplicate turns.
-- Apply as the database administrator; adjust the role if provisioning uses another name.
GRANT UPDATE (attrs) ON TABLE agents.activity_event TO agents_writer;
