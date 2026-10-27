-- Marketing signals DB (final: variant C + debate amendments). Idempotent: safe to run repeatedly.
-- 5 tables: companies, events, people, scrapes, appearances.  3 views: companies_current, company_history, tech_seen.
-- RLS on, no policies: only the service_role key (server-side) can read/write.
-- Convention: NULL = unknown (fetch/extractor failed). 0 or '{}' = checked and found none.

create table if not exists companies (
  id              bigint generated always as identity primary key,
  domain          text not null unique,          -- registrable domain: "acme.com"
  name            text,                          -- link-text fallback, written only in discover mode
  name_override   text,                          -- owner correction; wins over everything
  tech_ignore     text[] not null default '{}',  -- owner correction: tools wrongly detected for this company
  merged_into     bigint references companies(id),  -- owner merge: duplicate points at the kept row
  first_seen_at   timestamptz not null default now(),
  last_scraped_at timestamptz                    -- null = discovered, never scraped. Used for re-enqueue decisions.
);

create table if not exists events (
  id             bigint generated always as identity primary key,
  event_key      text not null unique,           -- see identity rules (§3)
  kind           text not null default 'event'
                 constraint events_kind_check check (kind in ('event', 'webinar', 'funding', 'earnings', 'press')),
  title          text,
  title_key      text generated always as (lower(regexp_replace(coalesce(title, ''), '[^a-zA-Z0-9]+', '', 'g'))) stored,
  url            text,
  event_date     date,                           -- gatherings: starts on. funding/earnings/press: announced on. null = undated
  location       text,
  ends_on        date,                           -- gatherings: last day (null = unknown or single day)
  amount_usd     bigint,                         -- funding only
  round          text,                           -- funding only: 'seed', 'series a', ...
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  last_scrape_id bigint                          -- provenance (FK added below; scrapes is created later)
);

alter table events add column if not exists ends_on date;  -- databases created before event pages were extracted

create table if not exists people (
  id             bigint generated always as identity primary key,
  person_key     text not null unique,           -- "li:<slug>" | "nm:<name_key>@<domain>" | "nm:<name_key>@event:<id>"
  name           text not null,
  job_title      text,                           -- latest seen; per-event history lives on appearances
  linkedin_url   text,
  company_id     bigint references companies(id) on delete restrict,
  company_name   text,
  merged_into    bigint references people(id),
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  last_scrape_id bigint
);

create table if not exists scrapes (
  id               bigint generated always as identity primary key,
  batch_id         uuid,
  parent_id        bigint references scrapes(id) on delete set null,  -- company scrapes fanned out from an event page
  url              text not null,                -- as submitted
  final_url        text,                         -- after redirects
  kind             text constraint scrapes_kind_check check (kind in ('company', 'event')),  -- null while queued; owner may preset
  kind_reason      text,                         -- which classify rule fired, e.g. 'host:lu.ma', 'jsonld:Event'
  status           text not null default 'queued'
                   constraint scrapes_status_check check (status in ('queued', 'ok', 'error')),
  error            text,
  company_id       bigint references companies(id) on delete restrict,
  event_id         bigint references events(id) on delete restrict,
  created_at       timestamptz not null default now(),
  scraped_at       timestamptz,                  -- when the job finished (ok or error)
  -- snapshot written in ONE final update with status='ok'. History = these rows over time.
  title            text,                         -- page title (event scrapes: event title snapshot)
  event_date       date,                         -- event scrapes: event date snapshot
  name             text,                         -- company profile snapshot ...
  description      text,
  logo_url         text,
  linkedin_url     text,
  x_url            text,
  github_url       text,
  facebook_url     text,
  instagram_url    text,
  youtube_url      text,
  tech             text[],                       -- null = tech scan failed; '{}' = none found
  ats              text,
  careers_url      text,
  open_roles       int,                          -- null = ATS lookup failed / no ATS; 0 = no openings
  latest_post_date date,
  posts_last_90d   int,
  raw_path         text,                         -- reserved: stored raw HTML (populated by a later issue)
  constraint scrapes_one_subject check (company_id is null or event_id is null)
);

do $$ begin
  alter table events add constraint events_last_scrape_fk foreign key (last_scrape_id) references scrapes(id) on delete set null;
exception when duplicate_object then null; end $$;
do $$ begin
  alter table people add constraint people_last_scrape_fk foreign key (last_scrape_id) references scrapes(id) on delete set null;
exception when duplicate_object then null; end $$;

-- Who appeared at / is the subject of an event. Exactly one of company_id / person_id.
create table if not exists appearances (
  id                bigint generated always as identity primary key,
  event_id          bigint not null references events(id) on delete cascade,
  company_id        bigint references companies(id) on delete restrict,
  person_id         bigint references people(id) on delete restrict,
  role              text not null
                    constraint appearances_role_check check (role in
                      ('sponsor', 'exhibitor', 'partner', 'organizer', 'speaker', 'listed', 'host', 'subject')),
  evidence          text,                        -- heading text or rule that produced the row, e.g. 'Gold Sponsors'
  job_title         text,                        -- person rows: title as printed on this page
  company_name      text,                        -- person rows: employer as printed on this page
  person_company_id bigint references companies(id) on delete set null,
  confirmed         boolean,                     -- owner: null = as extracted, true = checked, false = wrong (hidden)
  last_scrape_id    bigint references scrapes(id) on delete set null,
  first_seen_at     timestamptz not null default now(),  -- never in an upsert payload
  last_seen_at      timestamptz not null default now(),
  constraint appearances_one_party check ((company_id is null) <> (person_id is null)),
  constraint appearances_dedup unique nulls not distinct (event_id, company_id, person_id, role)
);

create index if not exists scrapes_batch_idx      on scrapes (batch_id);
create index if not exists scrapes_parent_idx     on scrapes (parent_id);
create index if not exists scrapes_company_ok_idx on scrapes (company_id, scraped_at desc, id desc) where status = 'ok';
create index if not exists scrapes_event_idx      on scrapes (event_id);
create index if not exists scrapes_queued_idx     on scrapes (created_at) where status = 'queued';
create index if not exists appearances_company_idx on appearances (company_id);
create index if not exists appearances_person_idx  on appearances (person_id);
create index if not exists events_kind_date_idx    on events (kind, event_date);
create index if not exists events_date_title_idx   on events (event_date, title_key);
create index if not exists people_company_idx      on people (company_id);

alter table companies   enable row level security;
alter table events      enable row level security;
alter table people      enable row level security;
alter table scrapes     enable row level security;
alter table appearances enable row level security;

drop view if exists companies_current;
drop view if exists company_history;
drop view if exists tech_seen;

-- a minus b, keeping a's order (EXCEPT has no defined output order).
create or replace function array_minus(a text[], b text[]) returns text[] language sql immutable as $$
  select array(select t from unnest(a) with ordinality as u(t, o) where not (t = any(coalesce(b, '{}'))) order by o)
$$;

-- Owner merge: move everything from the duplicate onto the kept company, then mark the duplicate.
-- Without this, rows still pointing at the duplicate would vanish from every query that joins companies_current.
create or replace function merge_company(dup bigint, keep bigint) returns void language plpgsql as $$
begin
  if dup = keep then raise exception 'cannot merge a company into itself'; end if;
  -- the same fact already recorded for the kept company wins
  delete from appearances a where a.company_id = dup and exists (
    select 1 from appearances k where k.event_id = a.event_id and k.company_id = keep and k.role = a.role);
  update appearances set company_id = keep where company_id = dup;
  update appearances set person_company_id = keep where person_company_id = dup;
  update people set company_id = keep where company_id = dup;
  update scrapes set company_id = keep where company_id = dup;
  update companies set merged_into = keep where merged_into = dup;  -- earlier duplicates follow along
  update companies set merged_into = keep where id = dup;
end $$;

-- One row per (unmerged) company. Profile from the latest ok scrape; tech and hiring from the latest ok scrape
-- where that reading was known, so one failed ATS call or tech scan never blanks the current row.
create view companies_current with (security_invoker = true) as
select c.id, c.domain,
       coalesce(c.name_override, p.name, c.name) as name,
       p.description, p.logo_url, p.linkedin_url, p.x_url, p.github_url,
       p.facebook_url, p.instagram_url, p.youtube_url,
       c.first_seen_at, c.last_scraped_at, p.id as scrape_id,
       case when t.tech is not null then array_minus(t.tech, c.tech_ignore) end as tech,
       h.ats, h.careers_url, h.open_roles, h.scraped_at as hiring_as_of,
       p.latest_post_date, p.posts_last_90d
from companies c
left join lateral (select * from scrapes s where s.company_id = c.id and s.status = 'ok'
                   order by s.scraped_at desc, s.id desc limit 1) p on true
left join lateral (select s.tech from scrapes s where s.company_id = c.id and s.status = 'ok' and s.tech is not null
                   order by s.scraped_at desc, s.id desc limit 1) t on true
left join lateral (select s.ats, s.careers_url, s.open_roles, s.scraped_at from scrapes s
                   where s.company_id = c.id and s.status = 'ok' and s.open_roles is not null
                   order by s.scraped_at desc, s.id desc limit 1) h on true
where c.merged_into is null;

-- One row per ok company scrape, with the previous KNOWN value of each signal alongside.
-- tech_added / tech_removed are null (not "everything") when either side's tech reading is unknown.
create view company_history with (security_invoker = true) as
select h.*,
       case when h.tech is not null and h.prev_tech is not null
            then array_minus(h.tech, h.prev_tech) end as tech_added,
       case when h.tech is not null and h.prev_tech is not null
            then array_minus(h.prev_tech, h.tech) end as tech_removed
from (
  select s.company_id, s.id as scrape_id, s.scraped_at, s.url,
         (select p.name from scrapes p where p.company_id = s.company_id and p.status = 'ok' and p.name is not null
             and (p.scraped_at, p.id) < (s.scraped_at, s.id) order by p.scraped_at desc, p.id desc limit 1) as prev_name,
         s.name, s.description,
         (select array_minus(p.tech, c.tech_ignore)
            from scrapes p where p.company_id = s.company_id and p.status = 'ok' and p.tech is not null
             and (p.scraped_at, p.id) < (s.scraped_at, s.id) order by p.scraped_at desc, p.id desc limit 1) as prev_tech,
         case when s.tech is not null then array_minus(s.tech, c.tech_ignore) end as tech,
         (select p.ats from scrapes p where p.company_id = s.company_id and p.status = 'ok' and p.ats is not null
             and (p.scraped_at, p.id) < (s.scraped_at, s.id) order by p.scraped_at desc, p.id desc limit 1) as prev_ats,
         s.ats,
         (select p.open_roles from scrapes p where p.company_id = s.company_id and p.status = 'ok' and p.open_roles is not null
             and (p.scraped_at, p.id) < (s.scraped_at, s.id) order by p.scraped_at desc, p.id desc limit 1) as prev_open_roles,
         s.open_roles, s.latest_post_date, s.posts_last_90d
  from scrapes s join companies c on c.id = s.company_id
  where s.status = 'ok'
) h;

-- One row per (company, tool) from known tech readings. adopted = an earlier ok scrape with a KNOWN tech
-- reading lacked the tool, i.e. we witnessed the change rather than seeing it on our first look.
create view tech_seen with (security_invoker = true) as
with obs as (
  select s.company_id, t.tool, s.scraped_at, s.id
  from scrapes s
  join companies c on c.id = s.company_id
  cross join lateral unnest(s.tech) as t(tool)
  where s.status = 'ok' and s.tech is not null and not (t.tool = any(c.tech_ignore))
), first_obs as (
  select distinct on (company_id, tool) company_id, tool, scraped_at, id
  from obs order by company_id, tool, scraped_at, id
)
select o.company_id, o.tool, min(o.scraped_at) as first_seen_at, max(o.scraped_at) as last_seen_at,
       exists (select 1 from scrapes p
               where p.company_id = f.company_id and p.status = 'ok' and p.tech is not null
                 and not (f.tool = any(p.tech)) and (p.scraped_at, p.id) < (f.scraped_at, f.id)) as adopted
from obs o join first_obs f using (company_id, tool)
group by o.company_id, o.tool, f.company_id, f.tool, f.scraped_at, f.id;

drop table if exists scraped_pages;  -- held only test rows (PRD §7)
