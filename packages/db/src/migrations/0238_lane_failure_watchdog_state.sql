CREATE TABLE "lane_failure_watchdog_state" (
	"company_id" uuid PRIMARY KEY NOT NULL,
	"state" jsonb NOT NULL,
	"cursor" jsonb,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
DO $$ BEGIN
	ALTER TABLE "lane_failure_watchdog_state" ADD CONSTRAINT "lane_failure_watchdog_state_company_id_companies_id_fk" FOREIGN KEY ("company_id") REFERENCES "public"."companies"("id") ON DELETE no action ON UPDATE no action;
EXCEPTION
	WHEN duplicate_object THEN NULL;
END $$;
