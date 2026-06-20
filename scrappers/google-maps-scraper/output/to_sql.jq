def s: if . == null or . == "" then "null" else "'" + (tostring | gsub("'";"''")) + "'" end;
def n: if . == null then "null" else tostring end;
"(" + ($cat|s) + "," + (.title|s) + "," + (.phone|s) + "," + (.web_site|s) + ","
    + ((.emails[0] // null)|s) + "," + (.address|s) + ","
    + ((.review_rating // null)|n) + "," + ((.review_count // null)|n) + ","
    + ((.latitude // null)|n) + "," + ((.longtitude // null)|n) + ","
    + (.link|s) + "," + ($q|s) + "," + ($loc|s) + ")"
