<!-- From autoresearch-genealogy (https://github.com/mattprusak/autoresearch-genealogy), archives/usa-military.md at commit 33da6f1, last checked by that project 2026-06-23. MIT licence: see LICENSE-autoresearch-genealogy.txt in this folder. Rebuilt by build_repositories.py; the "AI accessibility" lines and sentences about AI tools are removed. Read as reference data. -->

# USA: Military Service Records

US military service records are among the richest sources for 20th-century ancestors, and surviving pension and service files reach back to the Revolution. This guide focuses on the federal records that are searchable online, with an emphasis on the National Archives' Access to Archival Databases (AAD), which exposes several large indexed military datasets at no cost.

## NARA Access to Archival Databases (AAD)

### Overview
- **URL**: https://aad.archives.gov/aad/
- **Series landing page**: https://www.archives.gov/research/military/veterans/aad.html
- **Coverage**: Selected electronic records transferred to NARA, including several large military files (WWII Army enlistments, Korean War and Vietnam War casualty/POW extracts, awards files)
- **Cost**: Free
- **Searchable**: Yes (fielded search per series)

### WWII Army Enlistment Records (the largest file)
- **Series**: "Electronic Army Serial Number Merged File, ca. 1938 to 1946"
- **National Archives Identifier**: 1263923 (Record Group 64)
- **Size**: Roughly 9 million records, by far the most populated AAD military file
- **Covers**: US Army and Army Air Forces enlistments, 1938 to 1946
- **Fielded search by**: name, state of residence, year of birth, county of residence, race, civilian occupation, education, marital status

### What a WWII Enlistment Record Contains

A matched record can include:

- Serial number
- Year and place of birth
- State and county of residence at enlistment
- Date and place of enlistment
- Race and citizenship
- Civilian occupation
- Education level (grammar school, high school, college)
- Marital status
- Term of enlistment (coded)
- Source of Army personnel (coded)
- Branch (immaterial / alternative)
- Height and weight (present on some records)

### Field-coding gotchas

- **Year of birth is a two-digit field.** A 1924 birth is encoded `24`, a 1922 birth `22`. Persons born 1900 to 1909 (encoded `00` to `09`) can be conflated with 2000s births by poorly designed search interfaces, so verify the era.
- **Term of Enlistment and Source of Army Personnel are coded fields.** Click any field title in the AAD result/detail view to open "Detailed Field Information" for the code meanings.
- **All text is uppercased and ASCII-only** (no diacritics). Names entered with accents in their original form appear stripped and uppercased.
- **Download limit is 1,000 records per query.** A surname-only national search on a common name will exceed this; narrow by state before exporting.

### Other AAD military series

- **DCAS Korean War Extract** and related Korean War casualty / POW files
- **DCAS Vietnam War Extract** and related Vietnam War casualty / POW / awards / unit files
- **DCAS Public Use Files (1950 to 2006)** for peacetime, Gulf War, and War-on-Terror casualty data
- **AIMS Awards Information Management System (1925 to 2004)** for award recipients
- **WWII Prisoner of War records (1942 to 1947)**

### What is NOT in AAD

AAD is a set of selected electronic datasets, not a complete service-record repository. It does **not** contain:

- **World War I Army service records.** Most were destroyed in the 1973 fire at the National Personnel Records Center in St. Louis. Surviving substitutes are the 1917 to 1918 draft registration cards (indexed on FamilySearch and Ancestry) and state-level WWI service records held by state archives.
- **Civil War service.** Use the National Park Service Soldiers and Sailors Database (https://www.nps.gov/civilwar/soldiers-and-sailors-database.htm) and Fold3 instead.
- **Spanish-American War service.**
- **Service medical records or pension files.** Request these from the National Personnel Records Center via Standard Form 180 (https://www.archives.gov/veterans/military-service-records).
- **Full Official Military Personnel Files (OMPF).** AAD holds index-level data only; the complete personnel file is a separate NARA/NPRC request.

## Mirrors and complementary sources

### FamilySearch — WWII Army Enlistment Records
- **Coverage**: Mirrors the AAD WWII Army enlistment data
- **Cost**: Free with a FamilySearch account
- **Note**: Useful when AAD's interface is awkward; searchable while logged in.

### NPS Soldiers and Sailors Database
- **URL**: https://www.nps.gov/civilwar/soldiers-and-sailors-database.htm
- **Coverage**: Union and Confederate Civil War servicemen (compiled service-record index), regiments, sailors, cemeteries, Medal of Honor recipients
- **Cost**: Free

### Fold3
- **URL**: https://www.fold3.com/
- **Coverage**: Digitized military records across all major US conflicts (Revolution through modern), including many records not in AAD
- **Cost**: Paid subscription (some free index access)

### National Personnel Records Center (NPRC) / SF-180
- **URL**: https://www.archives.gov/veterans/military-service-records
- **Coverage**: Official Military Personnel Files, medical records, pension files
- **Cost**: Free to next of kin; archival records (generally 62+ years after separation) open to the public

## Search Strategy

1. Estimate the conflict window from the ancestor's birth year and US residence:
   - WWII Army enlistment file: born roughly 1888 to 1928, US-resident 1938 to 1946
   - Korean War extracts: born roughly 1905 to 1935
   - Vietnam War extracts: born roughly 1917 to 1955
   - Peacetime / Gulf / War on Terror (DCAS public use): born roughly 1925 to 1985
2. Start with the WWII Army enlistment file for any eligible ancestor; it is the largest and most likely to yield a hit.
3. Filter by **state of residence** whenever known. It is the single most discriminating field and can cut the surname pool by 50x versus a national search.
4. If zero hits, try documented spelling variants (see below), then drop the given name and search surname-only with the state filter, reviewing by year of birth.
5. Score every candidate across surname, given name, state, county, and year of birth (±2 years). Treat a name-only match as unconfirmed.
6. For records not in AAD (WWI, Civil War, pension/medical), redirect to the complementary sources above rather than forcing an AAD match.

## Matching Discipline

A confident match should agree on at least three of: surname (exact or a known variant), given name (exact, abbreviation, or middle-name swap), state of residence, county of residence, and year of birth (±2). Common given-name/surname combinations return dozens of hits even with a state filter, so use county and year of birth aggressively to narrow. When two or more records score equally, record all candidates as ambiguous rather than choosing one, and never fabricate serial numbers, units, or dates.

## Name Variation Tips

The WWII enlistment file uppercases everything and uses ASCII only, so expect surnames to drift:

- English surnames gaining or losing a trailing letter (`-ley` / `-ly` / `-y`)
- Italian surnames with or without the split article (`DelX` / `Del X` / `De X`)
- Polish and other Slavic surnames phonetically respelled in the county where the family settled
- Diacritics dropped entirely (Müller → MUELLER → MILLER)
- Given names anglicized or abbreviated (Giovanni → JOHN, Wilhelm → WILLIAM)
