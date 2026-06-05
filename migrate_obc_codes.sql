-- Migrate OBC column from text labels to 3-bit binary codes (3B|2B|1B)
-- 000=Empty, 001=1B, 010=2B, 100=3B, 011=1B&2B, 101=1B&3B, 110=2B&3B, 111=Loaded

UPDATE plays SET obc = CASE obc
    WHEN 'Empty' THEN '000'
    WHEN '1B'    THEN '001'
    WHEN '2B'    THEN '010'
    WHEN '3B'    THEN '100'
    WHEN '1&2B'  THEN '011'
    WHEN '1&3B'  THEN '101'
    WHEN '2&3B'  THEN '110'
    WHEN 'BL'    THEN '111'
    ELSE obc
END
WHERE obc IN ('Empty','1B','2B','3B','1&2B','1&3B','2&3B','BL');

UPDATE scrimmage_plays SET obc = CASE obc
    WHEN 'Empty' THEN '000'
    WHEN '1B'    THEN '001'
    WHEN '2B'    THEN '010'
    WHEN '3B'    THEN '100'
    WHEN '1&2B'  THEN '011'
    WHEN '1&3B'  THEN '101'
    WHEN '2&3B'  THEN '110'
    WHEN 'BL'    THEN '111'
    ELSE obc
END
WHERE obc IN ('Empty','1B','2B','3B','1&2B','1&3B','2&3B','BL');
