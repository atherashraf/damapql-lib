Drop materialized view pak_boundary_tree_mv;

alter table pak_boundary_tree Add COLUMN id Serial;
Update  pak_boundary_tree_vw t1 set parent_id = (Select id from pak_boundary_tree_vw t where t.name = t1.parent limit 1);

create materialized view pak_boundary_tree_mv as
SELECT '0' || row_number() over () as id,
       t0.adm0_en              AS name,
       'national'::text        AS level_name,
       0                       AS level,
       NULL::character varying AS parent,
        -1 AS parent_id,
       st_union(t0.geom)       AS geom
FROM pak_boundary_tehsil t0
GROUP BY t0.adm0_en
UNION
SELECT  '1' || row_number() over () as id, t1.adm1_en        AS name,
       'province'::text  AS level_name,
       1                 AS level,
       t1.adm0_en        AS parent,
       -1 AS parent_id,
       st_union(t1.geom) AS geom
FROM pak_boundary_tehsil t1
GROUP BY t1.adm1_en, t1.adm0_en
UNION
SELECT '2' || row_number() over () as id, t2.adm2_en        AS name,
       'district'::text  AS level_name,
       2                 AS level,
       t2.adm1_en        AS parent,
       -1 AS parent_id,
       st_union(t2.geom) AS geom
FROM pak_boundary_tehsil t2
GROUP BY t2.adm2_en, t2.adm1_en
UNION
SELECT '3' || row_number() over () as id, t3.adm3_en        AS name,
       'tehsil'::text    AS level_name,
       3                 AS level,
       t3.adm2_en        AS parent,
       -1 AS parent_id,
       st_union(t3.geom) AS geom
FROM pak_boundary_tehsil t3
GROUP BY t3.adm3_en, t3.adm2_en;