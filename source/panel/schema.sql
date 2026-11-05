CREATE TABLE `journal` (
	`id` text PRIMARY KEY NOT NULL,
	`owner_id` text NOT NULL,
	`kind` text NOT NULL,
	`host` text NOT NULL,
	`category` text NOT NULL,
	`state` text NOT NULL,
	`title` text NOT NULL,
	`detail` text NOT NULL,
	`evidence` text NOT NULL,
	`related_id` text,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `journal_owner_date` ON `journal` (`owner_id`,`created_at`,`id`);