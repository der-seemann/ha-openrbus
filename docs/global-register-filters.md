# Global register filters: reviewed mapping

The optional cooling, screed-drying, and cascade filters are installation-wide and default off. The mapping in
`custom_components/openrbus/optional_register_filters.py` is an explicit set
of exact CANopen index/subindex identities. It does not inspect label
fragments, infer membership from neighboring objects, or use manufacturer
navigation categories. The catalog remains responsible for deciding which
registers are projected for each discovered device. Core's family evidence
therefore scopes family-known rows (EHC-16, SCB-10, and MK-3); entries without
family evidence are filtered only if the current node's runtime capability
actually exposed that exact object.

## Cooling

The positive set contains the heat-pump cooling limits and setpoints
(`2303:00`, `234f:00`, `2350:00`, `2354:00`, `2355:00`, `23a7:00`,
`23b3:00`, `23b4:00`, `23cb:00`, `23cc:00`, `23d8:00`, `23d9:00`), explicit
cooling configuration and capacity values (`3011:00`, `301e:00`, `301f:00`,
`303c:00`, `30f3:00`–`30f5:00`, `30f9:00`, `30fa:00`, `30ff:00`, `3103:00`),
cooling-specific control terms (`3218:00`–`321a:00`, `321d:00`), zone cooling
activities and setpoints (`3411:00`, `3412:00`, `341a:00`, `341b:00`,
`3460:00`, `3466:00`, `346b:00`), and buffer/cascade cooling configuration
(`3504:00`, `370a:00`, `370e:00`, `384b:00`–`384e:00`, `5724:00`).

Cooling hardware and outcome values are included where the OBD function
identifies the heat-pump free-cooling valve (`430f:00`), heat-pump flow
setpoint (`4321:00`, HM033), cooling energy consumption/production
(`5046:00`, `5087:00`, `512e:00`, `5132:00`, `513c:00`, `5140:00`, `5149:00`),
or hours in cooling mode (`530f:00`). This covers the user examples: EHC-16
HM033, MK-3 cooling energy consumption and production, SCB-10 BP004 buffer
cooling setpoint, and zone floor-cooling setpoint CP270. Family-specific
availability comes from the Core catalog; for example `4321:00` is EHC-16,
`3504:00` and `370a:00` are SCB-10, and `513c:00`/`5140:00` are MK-3.

The map also contains a small number of cooling-only Core entries for which
the normalized family-evidence list is empty. These remain gated by actual
runtime capability on a node; an address does not make a new device or catalog
category appear.

## Screed drying

The positive set is `344d:00`–`344f:00` (CP470–CP490 screed duration and
temperature configuration), `3483:00`–`348c:00` (ZP000–ZP090, three program
steps, their start/end temperatures, and enable), and `5447:00`–`544a:00`
(ZM000/ZM010/ZM020/ZC000 current setpoint, start/end time, and remaining
duration). The OBD function text for ZP000 explicitly identifies the number
of days in the first screed-drying step. Core device evidence associates
ZP000–ZP090 and ZM000–ZC000 with EHC-16 and SCB-10; CP470–CP490 are filtered
only when exposed by a node catalog/runtime capability.


## Cascade: exact positive register audit

The positive map has 202 exact catalog heads. The table records each exact
address, its published Core 0.4.7 internal code where one exists, and the
short current Core English catalog meaning. The classification was checked
against OBD 1.47 datapoint identity and operational long-description: only
entries whose purpose identifies cascade configuration, producer/stage
coordination, cascade appliance status, or cascade pump/flow behavior are
included. This is a normalized review record, not a copied vendor description.
A dash means that Core has no internal code for that catalog entry; it does
not authorize synthesizing an entry. Core remains the sole source of rows and
runtime capabilities remain the sole source of which exact rows are projected.

| Address | Core code | Normalized Core meaning |
| --- | --- | --- |
| `2288:00` | GP033 | Heater on duration |
| `2289:00` | GP034 | Heater off duration |
| `228b:00` | GP046 | Cascade grad flow |
| `23b9:00` | HP166 | Primary ODU type |
| `23c4:00` | HP186 | Secondary ODU type |
| `2c01:00` | — | parCascadeNeutralBan |
| `30c5:00` | — | Cascade connectors |
| `30d1:00` | — | Cascade fix conn |
| `3700:00` | NP000 | Tout Sens cascade |
| `3701:00` | NP001 | CascProdMan Hys.High |
| `3702:00` | NP002 | CascProdMan Hys.Low |
| `3703:00` | NP003 | CascProdManErrRange |
| `3704:00` | NP004 | CascPFactorAlgoTemp |
| `3705:00` | NP005 | Cascade Permutation |
| `3706:00` | NP006 | Cascade Type |
| `3707:00` | NP007 | CascTOutdoorHeatParl |
| `3708:00` | NP008 | CascPrimPumpPostRun |
| `3709:00` | NP009 | CascInterStageTime |
| `370a:00` | NP010 | CascTOutdoorCoolPara |
| `370b:00` | NP011 | Cascade algorithm |
| `370c:00` | NP012 | CascPowerRiseTime |
| `370d:00` | NP013 | CascForceStop Pprim |
| `370e:00` | NP014 | Cascade Mode |
| `373a:00` | NP223 | Change permut type |
| `3741:00` | NP225 | Power ctrl strategy |
| `3754:00` | NP226 | Tsetpoint offset |
| `3779:00` | NP227 | First producer CH |
| `377a:00` | NP228 | Last producer CH |
| `377b:00` | NP229 | First producer DHW |
| `377c:00` | NP230 | Last producer DHW |
| `377d:00` | NP231 | Activation order |
| `377e:00` | NP281 | Producer switch time |
| `377f:00` | — | Identified producers |
| `3780:00` | NP282 | Late on value |
| `3781:00` | NP283 | Late off value |
| `3782:00` | NP284 | Early on value |
| `3783:00` | NP285 | Early off value |
| `3784:00` | — | Neutral band high T° |
| `3785:00` | — | Neutral band low T° |
| `3786:00` | — | Producer active time |
| `3787:00` | — | P factor inc heat |
| `3788:00` | — | I factor inc heat |
| `3789:00` | — | P factor dec heat |
| `378a:00` | — | I factor dec heat |
| `378b:00` | — | Time cons inc heat |
| `378c:00` | — | Time cons dec heat |
| `378d:00` | — | CascAddDelaySPC PC |
| `378e:00` | — | Number of entries |
| `378f:00` | — | CascTOffsetLLH |
| `3790:00` | — | T frost pump on |
| `3791:00` | — | T frost pump off |
| `3792:00` | — | T frost heat on |
| `3793:00` | — | T frost heat off |
| `3794:00` | — | T frost setpoint |
| `3795:00` | — | Current season mode |
| `3796:00` | NP286 | Release function |
| `3797:00` | NP287 | Prime pump type |
| `3798:00` | NP288 | Secondary pump type |
| `3799:00` | — | Pump anti seize time |
| `379a:00` | NP289 | Cascade max temp |
| `379b:00` | — | Min flow temp error |
| `379c:00` | — | Min flow temp error |
| `379d:00` | — | Max return temp err |
| `379e:00` | — | Min return temp err |
| `379f:00` | — | P factor inc heat |
| `37a0:00` | — | I factor inc heat |
| `37a1:00` | — | P factor dec heat |
| `37a2:00` | — | I factor dec heat |
| `37a3:00` | — | CascMinDeltaTMinLoad |
| `37a5:00` | NP295 | Min speed prime pump |
| `37a6:00` | — | Hydr. component |
| `37a8:00` | NP294 | Post run sec pump |
| `37a9:00` | — | CascAddDelayOnSPC PC |
| `37aa:00` | — | CascMinDeltaTMaxLoad |
| `37ab:00` | NP296 | Max speed prime pump |
| `37c8:00` | — | parCascadeTemperatur |
| `37c9:00` | — | parCascadeTemperatur |
| `37ca:00` | — | parCascadeTemperatur |
| `37cb:00` | — | parCascadeTemperatur |
| `3802:00` | EP001 | Minimum power |
| `3803:00` | EP086 | Maximum power (kW) |
| `3819:00` | EP023 | Control strategy HP |
| `381a:00` | EP024 | Late on value HP |
| `381b:00` | EP025 | Late off value HP |
| `381c:00` | EP026 | Early on value HP |
| `381d:00` | EP027 | Early off value HP |
| `381e:00` | — | Identified HP |
| `381f:00` | — | P factor inc heat |
| `3820:00` | — | P factor dec heat |
| `3821:00` | — | P factor inc heat |
| `3822:00` | — | P factor dec heat |
| `3823:00` | — | Time cons inc heat |
| `3824:00` | — | I factor inc heat |
| `3825:00` | — | I factor dec heat |
| `3826:00` | — | I factor inc heat |
| `3827:00` | — | I factor dec heat |
| `3828:00` | — | Time cons dec heat |
| `3829:00` | EP096 | Activation order HP |
| `382a:00` | EP146 | Preferred HP CH |
| `382b:00` | EP147 | Non preferred HP CH |
| `382c:00` | EP148 | Preferred HP DHW |
| `382d:00` | EP149 | Non preferred HP DHW |
| `382e:00` | EP150 | Activation order HP |
| `382f:00` | EP151 | HP switch time |
| `3830:00` | — | HP activation time |
| `3831:00` | EP152 | HP max temperature |
| `384b:00` | — | parCascadeHpCoolPFac |
| `384c:00` | — | parCascadeHpCoolPFac |
| `384d:00` | — | parCascadeHpCoolIFac |
| `384e:00` | — | parCascadeHpCoolIFac |
| `43a4:00` | HM132 | Primary ODU type |
| `43a5:00` | HM133 | Secondary ODU type |
| `510f:00` | AM107 | Cascade pump status |
| `514e:00` | — | Cascade components |
| `5164:00` | AC058 | Cascade pump starts |
| `5165:00` | AC059 | Cascade pump hours |
| `5700:00` | NM000 | CascadeNbProducer |
| `5702:00` | NM002 | CascTempoBetwStage |
| `5706:00` | — | Order switch counter |
| `5714:00` | — | CascTempoBtwStageCal |
| `5715:00` | NM021 | CascSystemTFAverage |
| `5716:00` | NM022 | CascNbStageAvailable |
| `5717:00` | NM023 | CascNbStageRequired |
| `571a:00` | NM026 | CascOutTempConnected |
| `571b:00` | NM027 | CascOutdoorTemp |
| `571c:00` | NM028 | CascNbProdPresent |
| `571d:00` | — | CascSysPowerReq |
| `571e:00` | — | CascSysPowerSetCalc |
| `5724:00` | — | Cascade with cooling |
| `5726:00` | — | On strategy value |
| `5740:00` | NM004 | Next producer |
| `5756:00` | — | Off strategy value |
| `5757:00` | NM111 | Total cascade power |
| `5758:00` | NM112 | Request cascade pwr |
| `5759:00` | NM113 | Num of act producers |
| `575a:00` | — | Producer pwr demand |
| `575b:00` | NM222 | Winning heat demand |
| `575c:00` | — | Anti frost pump |
| `575d:00` | — | Anti frost heat |
| `575e:00` | NM163 | Cascade state |
| `575f:00` | NM164 | Cascade flow temp |
| `5760:00` | NM165 | Cascade return temp |
| `5761:00` | NM166 | Casc primary pump |
| `5762:00` | NM167 | Casc secondary pump |
| `5763:00` | NM168 | Primary pump speed |
| `5764:00` | NM169 | Secondary pump speed |
| `5765:00` | — | Primary pump speed |
| `5766:00` | — | Secondary pump speed |
| `5767:00` | NM170 | Request power % |
| `5768:00` | — | Frost protection req |
| `5769:00` | NM171 | Temp active order |
| `576a:00` | NC000 | Cascade CH hours |
| `576b:00` | NC001 | Cascade DHW hours |
| `576c:00` | NM221 | Release input state |
| `576d:00` | — | Producer on power |
| `576e:00` | — | Producer off power |
| `576f:00` | — | Cascade data changed |
| `5771:00` | — | CascPrimePumpStatus |
| `5776:00` | NM226 | Primary pump speed |
| `577a:00` | NM227 | Secondary pump speed |
| `5803:00` | EM058 | Producer status |
| `5804:00` | EM068 | Prod Error priority |
| `5806:00` | EM078 | Flow temperature |
| `5807:00` | EM088 | Return temperature |
| `5808:00` | EM098 | Heat exchanger temp |
| `580a:00` | EM108 | Flue gas temperature |
| `580b:00` | EM118 | Service notification |
| `580c:00` | EM128 | Water pressure |
| `5810:00` | EM138 | 2nd return sensor |
| `582d:00` | EM148 | Second return temp |
| `582e:00` | EM158 | Pump speed |
| `582f:00` | EM168 | Actual rel power |
| `5835:00` | EM178 | Total burner hours |
| `5836:00` | EM188 | DHW burner hours |
| `5837:00` | EM238 | Error Time Stamps |
| `583a:00` | EM208 | Error code |
| `583c:00` | EM218 | Device type |
| `583d:00` | EM228 | Serial number |
| `583f:00` | EM248 | Max dT HEX producer |
| `5840:00` | EM012 | Cascade flow temp |
| `5841:00` | EM013 | Cascade return temp |
| `584a:00` | EM263 | Hp cascade state |
| `584b:00` | EM264 | HP cascade power |
| `584c:00` | EM265 | Requested HP power |
| `584d:00` | EM266 | Requested HP percent |
| `584e:00` | EM267 | Amount of HP |
| `584f:00` | EM268 | Heat demand HP |
| `5850:00` | EM269 | HP cascade flow temp |
| `5851:00` | EM270 | HP return temp |
| `5852:00` | — | Power request |
| `5853:00` | — | On strategy value |
| `5854:00` | — | Selected strategy |
| `5855:00` | — | HP power demand |
| `5856:00` | EM271 | Num of active HPs |
| `5857:00` | EM325 | Temp active order |
| `5858:00` | — | Order switch counter |
| `5859:00` | — | Cascade data changed |
| `585a:00` | EM321 | Next heat pump |
| `585d:00` | EM324 | Available HP |
| `586a:00` | EC000 | CH hours spend HP |
| `586b:00` | EC001 | DHW hours spend HP |
| `587f:00` | EM411 | HP cascade sub state |

The array head `378e:00` is in the exact map. Existing runtime subindices
`378e:01`–`378e:04` inherit its membership only when Core resolves the exact
head as a bounded array, the index is within `max_items`, there is no type
conflict, and the row's code, wire type, and storage match Core. No array rows
are generated. Cooling map arrays use the same proof; `346b:00` slot `:06` is
allowed because Core declares a ten-item array, despite narrower historical
family evidence. Scalar addresses retain exact `:00` matching.

### Cascade exclusions and overlaps

| Address | Decision | Operational boundary |
| --- | --- | --- |
| `23c5:00` | Excluded | OBD identifies this as shared between standalone and cascade arrangements; it is not cascade-only. |
| `50d9:00` | Excluded | Producer profile/version metadata has no independent cascade function. |
| `5705:00`, `5707:00`–`5713:00` | Excluded | Generic producer-manager communication/state without a precise cascade-only operation. `5706:00` is the documented permutation counter and is included. |
| `5772:00`–`5774:00` | Excluded | Generic producer-circuit pump-control input, output, and minimum-speed values; no cascade-specific role is established. |
| `5771:00` | Included | Exact primary SPC status at the low-loss header; OBD identity establishes the cascade's primary control state. |
| `3795:00` | Included | Exact `parCascadeCurrentSeasonMode` identity establishes the current cascade season mode; no standalone shared role is documented. |
| `370a:00`, `370e:00`, `384b:00`–`384e:00`, `5724:00` | Cooling + cascade | These exact addresses have both functions. Their projection requires both default-off preferences. |

Other general producer communication, generic IO/BMS, and hybrid-management
rows remain outside this map unless their exact OBD operational meaning
establishes a cascade-specific role. Names containing `Cascade` by themselves
are not sufficient. The explicit map is the entire inclusion boundary.

## Reviewed exclusions and ambiguous cases

The full semantic candidate scan was checked against the Core localized
catalog and OBD 1.47 datapoint name/description/function. The following
cooling-word matches are deliberately classified as neither filter:

| Addresses / register | Decision | Reason |
| --- | --- | --- |
| `2275:00` | Neither | Burner fan pre-purge cools the heat exchanger during forced DHW calibration. |
| `201c:00`, `2304:00`, `30d6:00`, `430e:00`, `434a:00`, `434b:00`, `540c:00` | Neither | Shared heating/cooling mapping, hysteresis, state, valve, mode, modulation, or bitfield; not a cooling-only entity. |
| `23a5:00`, `2a0f:00`–`2a15:00`, `2a43:00`, `2a49:00`–`2a4a:00`, `2a58:00`–`2a59:00`, `4a01:00` | Neither | Solar/DHW/CH tank or DHW heat-pump restart recooling, distinct from system cooling mode. |
| `342e:00`, `3613:00` | Neither | DHW zone release or pump delay prevents unintended tank/plate cooling; both belong to DHW operation. |
| `461a:00`, `461e:00`, `4639:00`, `463a:00`, `490e:03`–`490e:04` | Neither | Fuel-cell, water-treatment, or CHP engine coolant circuits, not building cooling mode. |
| `4858:06` | Neither (uncertain) | Inverter cooling-water temperature; current OBD description does not establish that this is the system cooling-mode function, so it is retained. |
| `AP059` | Neither | Alternate third-party mapping contains both heating and cooling encodings. |

Where evidence is mixed or describes a distinct cooling process, the
conservative result is “neither” and the register remains visible. The exact
positive set and this exclusion list are the review record; no complete
FunctionGroup-to-device assignment is required or implied.
