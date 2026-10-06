CREATE OR REPLACE FUNCTION `{project}.{dataset}.fn_is_canonical_execution_json_v1`(raw STRING)
RETURNS BOOL
LANGUAGE js
OPTIONS (description = 'Checks exact bounded Python-compatible canonical JSON object spelling.')
AS r"""
function fail() {
  throw new Error('invalid');
}

var PINNED_NFC_DATA = '0h$MZ2Ic_)<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#=m7!T0Rh|r0o(xr+yMdT0Rh+n0o(xr+yMdH0Rh|r0o(xr$^ik&0Rh|r0o(xr+yMdH0RhSZ0m=aZ+yMdH0Rh|r0o(xr+yMdH0Rh|r0o(xr+yMdH0Rh|r0RaI40RaI40RaKr0Rh|r0o(xr+yMdR0RiR#0p<Y#<^cib0RiR#0p<Y#<^cil0RiR#0o(xr+yMdH0RiR#0p<Y#<^ciR0Rh|r0_Fh$<^cib0Rh|r0o(xr+yMdH0RiR#0q6k%+yMdH0RiR#0qFq&>Hz`j0Ria&0qOw(>Hz`i0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#lmg}f0p<Y#<^cib0RiR#iUQmL0p<Y#<^cib0RiR#0o(xr<^cib0RiR#0p0-t+yMdR0RiR#0p<Y#<^cib0RiR#0o(xr+yMdH0Rh|r0o(xr+yMdR0RiR#0o(xr<^cib0Ri3t0ptMz<^cf;0Sf^P0Sy5T0S^HX0TBTb0TTfe0Tclh0Tu!m0vG`q0_Fh$+yMd-N#+3o<^cib0RiR#0p<Y#<^cib0RiR#0UiM#0U$9O0UQAx0UiM#0U!Y(0U`nB0RiR#0o(xr+yMdR0RiR#0p<Y#<^cib0Rh|r0p<Y#<^ciR0TCl+<^cib0RiR#0p<Y#<^cib0RiR#1Lgq%<^cib0RiR#0o(xr<^cod0RiR#0^9)s<^cib0Rh|rBqSf^0Rh|r0p<Y#<^ciR0RiR#0p<Y#+yMdH0Rh|r0p<Y#+yMdH0RiR#0o(xr<^cib0RiR#0o(xr<^ciR0RiR#0o(xr<^ciR0RiR#0p<ar0p<Y#<^cib0RiR#0p<Y#<^cib0Rh|r0p<Y;+yNQp0RiR#0p<Y#<^clc0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RrX$0p<Y#<^clc0RiR#0p<Y#<^cib0W91B0o(xr+yOo20Rh|r0o(xr+yMdR0RiR#0p<Y#<^e0_0RiR#0p<Y#<^cib0Rh|r0o(xr+yMdH0Rh|r0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^clS0RiR#0p<Y#+yMdR0RiR#0o(xr<^cib0RiR#0o(xr+yMdH0RbBU9045x<^cib0RiR#0o(xr<^cib0Rh|r0o(xr<^cib0RiR#0p<Y#<^eqi5eWq50Rh|r0p<Y#<^gC25eYHo0X_#232z4x32z4x34j3!ZwC<x2vh-EW(N@oZV3Si5ea<>Zf5~!0SOLk0c!zk0c&k`0d@fi4tfE40eS&?P22$i+yNZi0Rr3s0@wjKfdK)60Rn^p27&<rf&l@70Re&m1A+kp<^cib0RagD<^cib0YBUUaR&kk0SQyw0ni2J0RiR#0p<a<2MGZQ9|@iT2@B={%mL&9kOAHS0p<Y#+yUGH<^ciR0Z0iI<^cib0RiR#0p<Y#<^cib0RiR#0p<Y%+yOD>0RiR#0p<Y#<^cib0Rh|r0o(xr+yMdH0Rh|r0o(xr<^cib0Rh|r0^9)s+yMdR0RiR#0o(xr+yMdR0RiR#0p<Y#<^cib0Rh|r0p<Y#<^cib0RiR#W(N=nC*}bG+yMdR0RiR#0p<Y#<^cib0RiR#0p<ZW2>}T^2Mh@T2}B2(0p<Y#<^cib0RjO5+yMdH0Rh|r0o(xr+yMdR0RiR#0o(xr+yMdH0Rh|r0p<Y$0RaI40RaI40RaI40R`Lv2j&3;<^cib0mlL60RiR#0o(xr<^cib0RiR#0p<Y#<^cib0RiR#0o(xr<^cib0Rid(0oDNl+yMc~0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0q6k%<N*QX0Rh|r0onlp<^cie0Rh|r0p<Y#+yT)A<^cib0RaI40RiR#0p<Y#<^cib0RaI40RaI4<^cib0R`p(1OWj70p<Y#+yMdR0RaI40Rh|r0o(xr+yMdH0RiR#{}<)~0p<Y#<^hfY31Q{|0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0jmVs0RiLz0q6k%-T?vN0Ri9vY6t-c)$0M~0R`p(0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<ZA<^cib0a4}w0p<af0tqGwm;nhK<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0Xp0P0o(xr+yN#DU<VBe@B!ul0_Fh$<^ciR0R!d%0p<Y)<^cib0RrX$H3|0t39+658iEGq0RiR#0p<Y#<^cib0RiR#0p<Y#+yMdH0Rh|r0o(xr+yMdH0Rh|r0p<Y#<^j$J+yUbO+yRyW<^cib0RiR#0p<Y#<^hup+yMgS0V(DI0RaKr0R;)B0p<Y#+yTA><^cib0RiR#0p<aO1Lgq%<^fUM0Rh|r0o(yc+yMdH0RiR#0p<Y#<^ciR0RiR#0o(xr+yMdH0Rh|rGUfpR+yMdR0Rh|r!2t;>2@eT62>}O2<^cib0RiR#F$n<)KL<?-3I}Tm0SB`I2LTCD2LT5W2^r=A0p<Y#<^cib0RiR#0p<Y#<^cre0RiR#0p<Y#<^cib0nPyl1P2)A0b~gQ2mApE0SA8xcL@OpbP0|E2>}O#0to>L1qYo02~-Ia2~r8B0||oy2LcHJ2~r8F0|@~Mu9pD;0RaI40RaI4Jmvub<^cib0RiR#0p<Y#<^cib0lEnW0S2v^0RhJ;*Z~390RaI40RaI7;sF8J0Rh+n0oVZn*Z~390SVjz0o(xr+yMdH0Rh|r0o(xr+yMdH0R!d%0p<Y#<^cib0RiR#0o(xr+yNiv0RiR#0p<Y#<^h!f<^cib0RiR#yc^~L0p<Y#<^cib0RiR#0p<Y#<^clc0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0RiR#0p<Y#<^cib0R!d%0p<Y#<^cib0RiR#0p<Y#<^clc0RiR#0_Fh$<^cib0RiR#0p<Z^<^iDr<^cib0RiR#0p<Y#<^cib0RiR#_yXnuKIQ=d<^cib0RiR#{R8L$0q6k%+yMdR0pSPS0Rh|r0o(xr+yMdH0Rh|r0o(y@<^cib0RiR#0p<Y#<^cib0Rab%5WoQi{Q-ap0R{g7fC&KwfdYUD0R@8sfC&Kwg#w5P0R@KwiU|S*hXTL}0R@Eu@CX3~hXU{j0R@Qy@CX3~ivs!x0R@Eu=m-G?hXUvb0R@Qy=m-G?ivscp0tJNv<Ol%;g#z3N0R@Kw+z0^$i2~dR0R@W!+z0^$jRNEd0|kcy&Ikboi2}|D0R@W!&IkbojRM#R0R@Kw$Or=k{Q<xT0R{g7zz6{afdaq?0R@8szz6{ag#yS30R@Kw$_N4lhXQ~J0R@Euum}MKhXSw&0R@Qyum}MKivqd`0R@Eus0aZChXSYw0R@Qys0aZCivqF;0tJNvqzC~8g#w%i0R@KwoCpC0i2|Gm0R@W!oCpC0jRK?y0|kcyjtBt-i2{xY0R@W!jtBt-jRKem0R@KwhzJ4&ivp4e0R{a6hzJ1%zXFB`0R@2rhzJ1%!vcl~0R@Ev$Or)i#{$L(0R@Ev_6Gq4#{%>R0R@Qz^alY2%L4KT0R@c%`Ue38&jR=d0R@o*fCvEv(*phn0R@u-{09LA)dKnl0|l7_<Ocx-+XCVT0R^1{<Ocx--vZ(X0R^E0;s*f*;{xCZ0R^Q4pa=m4=K`Jx0R^c8<Ocx->jL5j0R^c8%m)Dl>jKIL0R^oC&<6np?*h&T0R^!G&IbVn^8(BV0R^=KiU<J(_X3Cr0R^`MyaxdV`2xBJ0|mVUwg&+P{{plJ0R_PWv<CqNg9EY$0R_bav<CqNhXb+)0R_ne^alY2iv#ip0R_zit_K7K(*v9b0R@!<ng;;|)dS)O0R@)>;0FQ)*#nRV0R@`_jt2n+-2>PM0R^7})&~Iv;RBur0R^K2oCgI3=>z-*0R^iA`UU|7?E}OI0R^uE!Uq8b@dJtn0R^)IhzA4(`2+9<0R_7Q?gjw`{R8j@0R_JU?gjw`fduvj0R_VY^acY3g9O?J0R_ba*aiUwhXkSr0R_nepa%g3iv;Kf0R_zi<^}-;jRejH0R_(k%mx7kkp#>J0R__o$_4=il?0Lp0R`6skOu(;nFQDd0R`Iw)&>Cun*@pn0R`Oyhz9`$p9Ihb0R`a$&ISVor3AJH0R`s+v<3kMsRXnJ0R`&=vIYSKtpv0N0R`^^vIYSKu>`mV0R{5|wgv$OwFI^X0R{I1v<3kMxdiM60R{U5>IMM?xdfaB0R{U5ng#&{xdfO70R{U5mIeU@y#$~J0R_DTkOlz-{{xN&0R_PXm<9m_g9Mfa0R_bbo(2I0hXkAkA_buZ_5}e2;{@~t4h6FX*aZOv^#s-h9R-&L{saL9*#-Or0R@W&`UC+5%?0=b0R@8w_5=Y1#Rc>P0R{U7@&o||y#??D0R`~`-UI;!vjf}&0R{B~)C2(qw*%4y0R{O3<^%x+y949|0R{a7$^-!gzXQkw0tK@K%me`i^#jTT0R@Z!$OHiei~+_30R^`N!~_8a`2)fP0|mVX%me`i{{_kf0R_1R$OHie`vt}X0R^=N`~(37_XYX{0R;oF1OWvDt^@%EZM*~l1;zrp1OWw$2Dk(S1=$9g1OWw`2ABi`1=9wQ1OWw=295*)1?dEi1OWxB1dId$1?vQg1OWxD1c(Fy1<M461OWw&1cU?u1^ot|1OWxV2Al)|1%U^i1OWxZ2Al)|1^ot=1OWxV29yK=1%U^a1OWxZ29yK=1^ot&1OWxV28;v&1%U^S1OWxZ28;v&1^EVs1OWxR2809w1^otw1OWxV2809w1^Wj60|5oS2K)m71^)*A0|5oW2K)m71^ov00|5oU2J`~~1%U_l0|5oY2J`~~1&Ifo1OWxh2ATu`1&arm1OWxj29^W^1*-?#0|5o?2HFD$1<41(0|5n#2fzaX1;q#c0|5nx2mAv81*ZkH0|5o;1hNAG1+4|J0|5o^1g--C1-%Ed0|5p92CxGG1p}x90R;o50|5oG2c!c51@i`?1C0U#e*pr20s>zF1b2l2Faq@g3Ic@<2L-tchXMfw7Ks7@0-p;31saV40R<e70s#dckpcn*CY1sL1umHa0R=Rh0s#edodOyKA;SUz1q;Oj0R;-x0s#dO*8%|r64?R)1r*x?0R=YQ0vZJ&g98Bt3xxv#1sRnC0R<120|5mInF9p{0iy!}1p}7@ECnk60|5mn@dE+{AcF)11p)m71qB?H1OWvWnFIj^DvtyU1p%c5AO!*C1Q!K0p9KL0H<1Mb1wW$&1O)+|1qB5|wFLnMNx2091vRe)7zG2(1px&E&jm^a)dKYf0R@)=_XZ9H{{oW-0R_PVl?MR@g9DQX0R_bZl?MX_fdiih0R_VXp$7v61EU841p}oA0R@W#rw0KA%>t<h0R@c%s|NuE&jPIn0|k=<y9WUU)dIZ-0R@)>w+8_Q*#fx-0R@i(y9WUU(E_~(0|f)a2LS~G#RmZe{Q}1a0R_JT$p--il>^fU0R`6r)dv9unFG%U0R`Iv(FXwqodeAW0R`Uz&j$eonFH4c0R`Iv*#`p!nFHSk0R`Iv;RmS#1qxyT1qfmR1xR241q5IL1xQ?)0R<}80Rja9+W`>;0sR5i1O)*m2n7Ka0|fyPBm{*4Hvt5N0XYE#g#kMO1bsaL1a&_F1a(0H1Z6`31YbpL1qT@c1ql`q1a&_F1a(0I1Ybo|1OXBQ1OyBt1cd=P0R)8sI{^fJJp%+~L+Swq0UQGb2N?kc2^J6pbw2?FbwM`;1BL-M1qT-a1qT@c1q&8@1p^#-1pz7t1pyoZ1p^!c1qTlS1pyR)1qT-a1qT@c1q&9A0R;gc0tE#b0R;gi0R;&H<pKl&=m81@0n`Bn1Odtc1q1=W0R;pBwgC+UQJ4V=1OovA1P1{D1b7qz1a=S!1Rnts1OZ421OYS!1OX}q1OXrg1OXNe1W^ZV1pyG?6$Jra0tEqD0tEq90tEq50tEq11O)*`DFp!w0tEpN0|f&W0R;mW0tEpZy#)pRcC~8(1;2K;YXJrScJ*rk1;KXrYXJp=cfo4`1;uv5YXJp^cd2Uu1<7`+YXJp;b&YHR1;ccYYyky}cZF;L1<iJcYykz2chPGB1<`iXYXJq5ce!f;1=V)DYXJq9chPGB1=)7fYXJqDcgbr31>JVbYXJp=bf;_q1%q^{Yyky_bg66s1&4I2YykzOchzeF1?P6xYXJqRchhSD1?zUzYXJr4Z>wwp1?O+AYykzYcdcvz1@U&TYykzaceiW-1@m^fYykzcceQK*1@(5fYykzgcY$mH1^IS^Yykzkce`u>1^srtYykzocY$mH1%Y>iYykzsclT=n1%-F{YXJqtclT=n1&4R}YXJqlb=hnI1^;y0Yykzycin6O1&epzYykz$cb#kj1&?>1Yykz)cZF;L1(SD&Yykz+ccp9r1(kQFYykxW=WGE51L<r51>1L#YykzGcav-Z1>bj_YykzKcb{wl1>tx3YykzMcl&Gs1?6|~YykzQck^rk1?hLaYykzUcfV`_1?zYDYykzWcl&Gs1@CvoYykzacgJi21@m{UYykzecdu*#1^0KkYykzice`u>1?_c}Z2<+Zb(d`c1@U!@Z2<+db&YKS1&4H>Z2<*`bfIkl1&ef{Z2<*~bfawn1&4U0Z2<+zcc*Ou1&er{Z2<+%cb{zm1&es0Z2<+%ccX0q1&?^`Yykz)ckgTg1p}{b0R;oGZ2<+Bc-d?L1=)AoYykzCc(H8(1>1MCZ2<+Hc>Qbv1>bl7Yykz6bh&K-1(S5UZ2<*~biHi>1&egQZ2<)f!EFHr3&U*z1*>?)Z2<-CcgJl31+RFKZ2<-Gcav=a1+#ehYyk!Jcl&Gs1-E#GZ2<-OcZY2O1-W>eZ2<-Qcb#nk1-*EOZ2<-UcZqEQ1;KccZ2<*?c#~}b1)p^7Z2<+Jbnk5e1)p@|Z2<+JbmeUU1<QExZ2<+1c=K%m1<!c3Z2<+5c(-i<1<`o_Z2<+7c!6#K1=V<gZUF_Cc!O>M1=)D_Z2<+Fc=>Gs1>Jc4Z2<+Jc>iqy1>tzfZ2<+Nc*|`81><;yZUF_Qc!zEQ1?PB&ZUF_Uc!_QS1?hN=ZUF_Wc#m!Y1?za5ZUF_Yc%5zm1@Cy>Z2<+bc;9US1@m~tZ2<+fc*ku41>1PZZ2<+vc%5zm1;KcmZUF_qc%5zn1-EpkZUqIsdHHPt1^;;aZ2<+rd9iK*1%r9BZUF_ucg1c21%-FVZUF_ycgt=81&MdfZUF_$cfD=_1&w#VZUF_)cg=1A1(A2pZUF@s({2F;7u9Y71=n`jZUF_?cH3?N1>1JtZUF_`cHwRT1>bhjZUF_~cGGSF1><(%ZUF`3cH?dV1uNff0R=1JZUF`5d8uvz1*dtdZUF`9dEssW1*>`EZUF`DdGT%m1+RJYZUF`7clmAs1*v!YZUF`BcmHky1+901Zvh4Gckylk1+jPYZUF`JcY$vK1+{mBZvh1xhi?G|8;NfL1^s#bZUF_qdH-$!1%Z0aZUF_udCzVE1^Ic=ZUF_mdDCtI1^s!6Zvh3rd5doW1^0KIZvh3lcb{(o1^aiSZvh3pccpIu1^;)IZvh3tcb9Jg1%r5{Zvh3xcc*Uw1sAJt0R<PWZvh4Kbg^#%1@m;XZvh4ObhmE-1^0BhZvh4Sbg6Fv1^aZXZvh4Wbh&Q<1^;xrZvh2^b^UGu1%q|}ZUF_IdVy~N1>bptZvh3Ndbw`_1><?UZvh4Ibj@!81@UyxZvh4MbklDE1@&~*Zvh4Qbj5E01^INxZvh4Ubk%PG1^sl_Zvh2?b(wDg1%Y*&Zvh3ddgE^a1@n33Zvh3hdZBLt1^0QQZvh3ldf9IQ1^ap1Zvh3pdg*Tg1^;>LZwCdPZP{-D1)pu%Zvh1ZgKz-_1BGw_1qX$20R;z#Z~+Aihi?G|3yE(51?_F=Zvh4GZRu|T1p||C0R;n<Z~+Aem2d$C2bXXG1q+vN0R;=0Zvh3hZTW8j1-EVaZvh1Zqi_KQ1Ep{Q1qY>Y0R;!Aa03N}Zi#RK1&3~lZ~+Aavv2_g1GR7g1qZco0R;!Qa03O=ZJBTZ1=DSrZ~+Aa!*Brw1I2Iw1qa1&0R;!gZ~+Ai$8P}z3(0Q*1)*-KZ~+CQZmDnq1q0J?0R;oqZ~+Ae)o=j?2iI@`1q;`20R;=$Zvh4EZMkp(1@CRSZ~+Aa<8T251Lbf51qbDD0R;!=Z~+Ai=WhW83+ZnG1-)*`Z~+CsZpm-~1q1VN0R;o~Z~+Ae^>6_N2lsFR1q=6Y0R;>BZvh2?ZrN}F1%qzcZ~+AagK+@`1BGz`1qX$30R;z#aRUX>Zs~9V1=DWnZ~+AalW_qB1C?<B1qYRJ0R;z_aRUXFZuxKl1($C5Z~+Aaqj3QR1Ep~R1qY>Z0R;!AaRCJjr*HuU3#o7d1?O&waRLPcwQ&Lk2e)wo1q-=w0R^pYnQ;LHuWp%f0R;oYaRCJb#c=@z2gPv#1qa7*0R;=kZ~+Ai$#4M${cfpo0R{hVsc``X1JiK<1q0P_0R;!uaRCJf*Kq*_3)gS~1q<150R{bT-*Eu~iEjY~`EKKJ0RoF}0R{PP=Wzi7jc)-3`EKiR0RoS20R{1H?{NVF*=_*^>2C9J0Rr1@0R`o5_i+IN-EIQ~{{i)I0R{g7_izCP{{i`M0R{g7`)~mT{{j7Q0R{g7|8M~X{{ew<0R{g7gK+@`{{e+@0R{g7hj9S~{{e|{0R{g7i*W%3{{fA00R{g7k8uG7{{fM40R{g7lW_qB-vO0z0R`UymvI3F-vOC%0R`Uyn{fdJ-vOO*0R`UypK$>N-vOa<0R`Uyqj3QR-vOm@0R`Uyr*Q!V-vOy{0R`Uyt8oDZ-vO<00R`UyuW<nd-vP040R`UyvvC0hf3<M|1%J130R?}#aRCK?yKw;pf4y-51%JPB0R?}-aRCK?!*Kxxf5mYD1%JnJ0R?}_aRCK?%W(k(f6Z|L1%J<R0R?~2aRCK?({TX>{cqE90R{hW*>V8|g8|ub0R@9_+i?K}k8s^_0tJb1=WziA0poE21<i2dasdU;aOrXZ1<`QxasdMAZ~+C?aP4sd0*P<~1+jenaRCLJ0r_zO1($I9aRCLZaQ$%t1)Xq*asdSagK_}{<#3O40Rn+>0R`o7lXC$AgK+@`>2Qs50R<bGa{&b%nR5XJADMCi1+8$Ca{&dfaG7%f1&?r}a{&T{aRUXpaHnzs1(|TEasdVXaHDep1^;lVa{&c`akFy)0-<pN1wy%V0R=$0a{&cDxpDyowQ#d@0R^{kxpM&pt#HG00RrW40R_cyp>qKR$8e!@0R_Qu$8rG$w{XdF0R@F|!*c-zhjGbs0R@S1({lj=xp4snm2t0g0R@SD*>eE`(QyF+oqhua>jBwv0R_`=+j0Q~%W&Ot0tMM{=W+oB0poH31+j7Oa{&U`aRCLPar1Kl0@rZ?1*LKAasdL7eFFjn0RjZ20s{GU1OmN-0RaNbfdOv?6T^7{1rf!069qHtc^?G^sd@ng46Awp1qQ8pH3b2edj$mnp?d=b0jGNy1p(K40tErvdm9A_k$eLM0hfFO1p}RY0tEq~d=~}9hyl-h0tErnd<z9X>3jhN<A?$4d;taFhym?<0R<QDd;tX)@q7aX1NVFZ1q1nf0|f*Bd;tXmfqe%B3y*yP1q+dV0|f(@eE|gnnSBEV1D|~X1p}ddBLx%ZeE|gt>3snO3G00U1qbbYF$Ko~&wc>~#{toP0R^D}(|!R3p#jx?2L*5AegOq<<$eJLZ|8mi1#jtoJOaKm0Rp}=vJV6T$DRT33Ize00Rja9mH`3<0g?d%1p$r$0tEqz0Rja9h5-Tv0fGSn1p$5n1p#^j1p#&f1p#sb1p#gY1p#OR1p#CN1p#0O1p!h41p`t71p!O}1p`b11p!6@1p`I`1pz<-1p`0=1pzt%1p_)41)Bj13IzcP4g~=v0tEpn0tEpr0tEpv0tEpz0tEp%0tEp*0tEp<0tEp@0tEp{0tEq00tEq40|fzB0tEqE0tEqI2L%Cb0R;nX0tEqc0R;nd0tEqi0R;nj0tEqo0R;np0tEqu0RaUAg#i}@n*p@}0|gJe0RaUMy#WCQ55EBc1rNai1O)-f0fLYN0`IZ|0RoxG1OWo?r~?54^{@j00_CU#0Rp9~1pxxVlm!6-)#U;K0@vjN0Rr#-1OWo^oC5&@hmHjS0*C(u0RpYu1OWociUa`yljZ{f0-?(T0RpYX0|5fFzykpShoS=k0;9YH0Rr={1OWp1pacN|@stDs0=3fv0Rr8g0|5fj`~m?2v8n|D0?n2M0RpkO1OWntoCE;^<I4j90)y-V0Ro}#1OWo=qyzy1tIY!e0{^}P0Rp$#1OWn@-2(vvkHiB30=wx10Rp4Q1OWp7t^@%B-IxRc0=uCD0RqjY1pxx>odp2`p_v5%0@u+50Rp?Z1OWoWoCE;^mx%-c0_Efb0Rr930|5fF%L4%dkF5g%0*i<P0Rofv0s#X5>;eG--GT%G0{Q*}0Rp-10|5f*)dK+n^_T+z0)guS0Rq*z0|5fXfdv5q{pbV%0>|?M0RoNV0|5fZn*;#@`GEuh0*kT(0Rp3m0|5f#fdv5q(enfW0`a^A0RrW%1OWp5s00B5zwiSA0<r7^0RoGR0|5g6nFRp?kK+RY0`Hv#0RoYp1pxx{`~v|3vF-x_0?pL}0RqR10|5fFw*vtJm)ZmY0<*mY0Rp|b0|5fbtOWrA@AU)$0-NOo0Rpp^1OWo$j|2e%mzf0t0?~m40RqdW1pxy2<^%x(v#kXI0=KdR0RrLA1OWn%@dE(@&z1!N0-J;d0Rq>F1OWoc)B^zmiMs;<0_(N}0Rn-i0|5flvjhPG>DdGU0-f3e0RpMl1OWn}kOTn&k*fs(0-c}*0RrEi1pxx_!vp~Wr}6^<0-4+c0Rq*!0|5fvo&x~_lbQtq0{fH&0Ro$V1pxx9>I4A-`_2Rb0*j^u0Rpvw1OWoEj{^Y$x%>hF0^#cd0Rr{v0s#Wun*{*^iOU250;#wJ0Rq*i0|5fF>;wS<{oe!u0-xRl0RrvQ1OWowsRRK6lc59w0+o~m0Rol(0|5fV?gIe=&*TFE0^_Lz0Rq+10|5f<r2_#1p_u~#0*Rgl0Rr=z1pxxVlLY|+t)m110=vTl0Rrv20|5fD>jVJ;+vWrT0@2t60Rru}1OWoi^#cI{<Cp{i0*9*w0RrEO1pxwu_XGg~;qL?i0-xpt0Rr`x1OWnxl>`9-spta%0@LLK0Rs7*0|5f}iUR=x)rkWE0=I?(0Rq#g1pxw&fCK>og`fig0{@Ex0Rpe%1OWo)tOWrA!J`EM0-N^)0RsE=1OWoc&;$Vj`Mv}J0^^?q0RsJv1OWnz(*pqljimzt0`2Gm0Rs8V1OWoCjspP#`<w*<0-4DK0RpSX1OWn(vIGGF-K+!w0)>|Z0Rrcd1OWoYg#-Zt{qh3=0^@}P0RqRN1pxxb-UI;xwT%M-0@0=g0Rs2(1OWoquLJ=C*{TBp0-5Oq0RpMw1OWnt#{>Zar<nu+0;`P!0Rrcq1pxw`mjwX=^YsJ)0-4VQ0RpAU1OWo`!~_8X`?CZA0=t(40Rn}P1OWos=>q`*ufziZ0*%B20Rp+80|5e$i~|7z*O~<Z0^_U%0Ro?w1pxxPpacN|t(61;0_)EM0Ro$i0|5fH_5uL{r|kj(0=dZq0Rq#o1OWn@-~#~y*Zczk0<Zf60RqkS0|5f_oCE;^!=(fP0>`=o0Rs7~1pxw&+XDdtiJt`l0`Z>(0Rpw{1OWow+ynsv+4KVe0{!^}0Rqjl1OWo0%>)4gm#YH-0{6xP0Rs1q0|5g4!2<yT@9YEt0+*u&0Rowk1p@->h6MrwyUqjy0_~s$0Rq>X1OWn>jRXM#w}S)$0`-Cf0RriQ1OWn<f&>8qi;DvR0=@ME0RqA40|Ek@&;tSj)4T%%0@<en0Rqva0|Wx~gaZKr-Gl=H0*8bH0Rr*y0s#V_r2_#1?~MZi0)wgr0Roey1pxxdqXhv1)13tY0-v1)0RqRH1pxxFk_7<*;gAIZ0-uis0Rr)d1pxx1h6Movt@i{00-N^)0RqwH1OWo8;{*W$;o<}V0-NCk0RrRG1OWoS(F6ek<IV&D0{zAW0Ro$|1OWoqumk}D$*cqc0<)t80Rp?A1OWoim;?a=^M(Wg0-J;c0Ro4F1OWnzgaiQs{euJn0`r3e0RrcP1OWnxg9HHriGu_I0;hlk0RpH00|5f7`U3$1#qk3H0-f;#0Rpe^0|5fh>jMD-tLXy)0^Q*Q0RqS00|5fb-va>xkJtkN0-wbL0Rol70|5fTy#oOP)4T%#0-?480Rp+T0|5fxssjN6-KYZr0*Q_T0Rq#B0|5fvhywuv>*oXk0)(Op0RpMv0|Nq+u>}DF_o4*>0+XZ#0RrW#1pxxxqy+&2>7xY!0^Obk0Rs7*1pxxJlLY|+$CL#D0>P670Rpj-1pxxXhy?)xlZOQX0`r6g0Rq*81pxxPfCT{pvHb)A0^jZg0Rq$R1OWoI>;wS<uj>Q>0^8;U0Rr*l1OWp7<pco&_2UEq0@LIK0Rpk&1OWoC;RFE!k=q0T0^`{P0Rs8h1OWo`)C2(nxzGdw0`<)V0Rr#M1OWp3%me`fx4Z-a0=K&a0RpSF1OWp1t^@%Bo38``0`IK^0Rs7{1OWouqyzy1>x%>d0@0xa0RrEj1OWo;odf{_-<Sje0=Jd~0RqpJ1OWn#k^}()i;@HZ0=JI@0Roqg1OWo)jsyV$y^RC`0@IBI0Rp{;1OWo|{{sO6+4}<l0_FGv0RpG>0|5f}@&f?^<?RCj0@>;V0Rq9@0|5ew+XDdt_sjzU0_nvA0RrK~0|5fV!~+2W^S=WD0)xK;0RrE@0|5f{z5@XQ{k{VM0-?VH0RoG@0|5f-yaNFO@wNj20+X%-0Ro|^0|5fnqyqs0ouLB(0+W#g0RqR50|5f#jROG!+lvDM0+ovc0RsPu0|5e|iUR=x_5K0@0^{rg0Rr~52>}A=wFv<Nh~x<Y0_pq(0Rr{v1pxxJ>IDG;=-mnd0`!Lq0Rs4s3;_bE?g9Y<mF)sV1c%iJ(bWe61liOF&(#MD1cB8D(bWe51cTKF(bWe51Ul8#2LS{))zt?91iRG-?bQbX1ijS<?bQbX1i#e>=hX)R1i#e>>D31T1i#e>>(vJV1i#e>?bQbX1i#e>@6`tZ1i#e>@zn<b1i#e>^VJ6e1i#e>_tggh1i#e>`PByj1i#e>`_%^l1i#e>{nZBn1i#e>|J4Tq1i#e>gVqNE1i#e>ht>xH1i#e>iPi@K1i#e>jn)SN1i#e>kJbkQ1i#e>lhy|T1i#e>mDUFV1i#e>m(~XX1i#e>nbrpZ1i#e>o7M*b1nboYqt*uj1ohPinbrpZ1mo2Qo7M*b1k=?Aoz@4;Dg^;R0tEp)4+RSzg8>Bw4FLrP51a%A2N?kc2^Q}H1p@&A1qBxZ1qlKC0R;mU0R;mW{RRaE52o+}1Pu-W1Pu%U1OXfY1Opra1O*%c1P2@e1PL5h1Oux90R#iB0RaR9odE#^1D^o_1P7l10R#u30l>@z0)?j)0RoYy6#)W)rxgJL!P^G`0>`2i0RpX|6#)X3p%noFiJ%n$0-K%{0RrEh6#)XpoD~5A_nZ|00^5xi0RpAR2LS?sn-u{9^_mp{0@s=q0Rpp{6#)Y4%Lf4hvyB!30@<1s0RoSj6#)X3%Lf4h|Ctp50_T|(0Row&6#)X<nH2#7%b67c0{NH&0RqpM6#)XZm=ys6|BMy^0?C&Z0Rpv`6#)X@mK6a4*OnCl0-cN&0Rp>~6#)Xdl@$R3g_RWn0^5`o0RqRA6#)XrlobI2r<4@|0>zUR0Rp#^6#)XLlNA91_mUL>0_Bnw0RrQa6#)X}k`)00=aLlx0*|-{0RoAi6afO)kre>~uaOl20;#hH0Ro$m6#)W~kre>~>5vrx0=teC0RoAT6#)Y4j}-v|t&bG}0=teC0Rokc6#)X{j1>U_--{Ii0>O(F0Rn-G6#)W+ixmL^+lmzd0=bG60RpRv6#)W|iWLC@>4X&l0*i?i0RroU6#)YIhZO+=wTBe}0+E>&0RqMS6afN<f)xP*y@3@00*!$c0RoeQ6#)XrfE57(%YYRD0@wZ&0Rrib2LS@l{S*NL$Ndxm0;~NL0Rn~n6afPF{1gEK`TG<B0`G<g0RruX2LS@>`4j;HkNFe<0{Qq90Rox&6afPN_Y?sF@r4!v0@sBW0RqAG6afO)^b`RC+4K|v0>}CW0Rq$V6afOO@)Q9AkMa}&0{QV20Rpl01_1))@e~09+3^$s0>R`I0RpA*6afP1gBAe-t?(290+a9*0Rr*v6afO+?-T(7t?C8=0`cq=0RpY+1_1(}>=Xe4r|c8~0*CAr0Rq436afOi>J$M2r-2p$0*&ev0Rp+{6afN}=@bD1yXX`F0?U9F0RoHM1_1(_fEED)wdWK80<q^50RpY(6afN<=M(_~`O^Xc0;A>>0Rq3(1_1)W)dm3qquLVz0^{Wr0RrXa6afOA{}llOy_W_70{hwo0Rrvh6afO=<P-q{@BS450)ylf0Ro@n6afN_;}ih`%i<IP0=?iA0Rqka6#)W^{uKcNo!}G!0-whQ0Rp?<6afOe-4p==wcQi}0<+x|0Rp+*6afO?+!O%<x!e>10`uDx0RoZS6afPL+Y|u;q1zMz0{hw&0Rr{f6afOm+7tl-ui6v=0<GB;0Ro5E6afOm*AxK)&D9hE0@t<%0Rs2c6afPJ))WB(rPUMx0`b%o0Rpkp6afO6vjzbIxzZE?0`=1r0Rpeo6afOK`V|2Jv(Xd*0*lcU0Rqp^6afOA(G&p!wfPkR0`bih0Rp+r6afPNq6Ps1&&w160;k6m0Ro506afP1_7wpF`^Xdl0+06<0Rq|g6#)YOoD~5A`<xX40;k6m0Rr*J6afO!;1dA?`_mNx0)fXA0RsER6afP9#uNbp`Nb3g0+GfP0Rpp>1_1)8_7wpF$-@)@0=2~y0Ro-E6afOc!4v@k>yZWl0;|Ck0RoG|6afOGzZ3xill2t=0?WP>0RoG?6afPNyA%Neg}D>~0+ERZ0Rp4A6afOO^A!OCo3|7J0+E3R0Rp+U6afOq@)ZFBfwmL@0;#nW0Rr{36afPDv=jjX)A<Df0@?2d0RsK^1pxxrvJ?RV{qO|=0{O5M0RrE!6afOQvJ?RV(XSK%0)wv<0Roe*6afN(uM`0Sv#t~Y0++570RsQ66afOy?*#z@rLPnL0<o(U0RpwE6afOQ?-c<8|ELrJ0*R><0Rs2v1pxx7tP}wP!KM@e0^{Zd0Rn~Q1pxw=r4#`Iv!fIN0_&p`0Rr=*6afPN?G*t6nWGc|0=1$P0RqFK6afO+p%eiE>(2)P0<E7E0Rr>d1pxwso)iHB=hX!P0`Hp?0Rq>X6afOGn-l>8waf(p0>_#Z0Ro?z6afO^#{~fbrNjjR0^61p0Rp?06afPJ=@kJ2i<T4t0_W)!0Rrji6#)XxlN130rIQo^0{xN{0Rq>O6afOykrV*}ujmy40-ul+0RsKA1pxx@j}!p{$+86j0*{Xr0Rpq$1_1)oj1&O^v!(?B0*9sr0Rr)(1pxws<rM(}?c@~!0+)#t0Ro$$1pxw|p#=c~)1U<b0->M<0Rqj46afOwh!g<=&xjNO0{e#)0Ro@o6#)YMh7<t;iR2Xl0_)-x0RrEY1pxy8gA@S*g@Y6U0*8VW0Rr3M6#)X*j0FJ#`Tr9E0`rOm0Ro?j1pxw${}TZM*ZmU#0@2_V0Rpl769EFB{1X8Jm;4g}0>A$R0Rrp#1OWo=`UC+2;rSB*0=wN60Rrpx1OWoK_7edDv-T4K0{Pq(0RqYM1OWo4@)H39joTFg0{`(70RoHi69EF#@e=_8$>#(C0+H|&0Rq?A6#)Xx?h^q5+3gbn0@dsj0Rs8h6#)X1-2?#w*W3gF0=3u`0Rp$$1OWo~=Mw<}@z?|b0>$SO0RsKy69EG0<r4t{!O{c)0<qBq0Rp+?69EFN&jbMio#Yb%0>PLD0Rq?76#)XD;}Zb_|KSq>0`b)q0RoNT69EFD`xF5JzsCdt0;R_U0Rron1_1)wmj(d>lim{n0*~Gk0Rr8p69EFn)D-~&``Z%%0*Txc0RrdS69EFfn-u{9!`l-90>Rr80Rpev69EF>+7kf+wYdZV0_EBh0Rs2g69EG4*b@N)+t(8T0`b`s0Rq3*69EG4))N5&kJS?a0-4zp0RpYp69EFN*AoE(jn@+a0^79&0Rr=|1OWn}vjhPG_t6yr0`1Zh0RrpN69EFJ(h~s!!IuO90*}!X0Rn-l1OWn}(G>v#o6!{k0{y210Rp|F1OWn#(G>v##my4|0<X;z0Ro%N69EF5%@Y9vo6Hjd0^iIN0Rq*^69EFR%M$?tgUJ&C0;kFo0Rp4R69EG6$P)nq&(9SB0=>r*0RsQV69EE|&lLdz#l;f=0>Q--0Rp>?1OWow!xI4l(Zdq~0;SFs0Ro-F69EF9lokO3xr77(0?UB}0Ro-O6#)W`%oPCw-MkY40>!%%0RoM=69EGGwG#mX)${`a0@1V+0RpGA69EGCvJ(LUzp)bm0@3ON0RpMV2LS?|uoD3Sy|5Dj0@tq-0RsKQ2LS?;tP=qO&8iat0-MkS0RrdG0|5e?r4s=H<D(M+0;Qu90RrvH0|5e+qZ0uF-JTNx0->A}0Ro|%69EF-of829<ChZw0<D)50RrW>0|5fXkrM#|v%wVs0`ZU&0Rr=}0|5e;z!d=k-;5Ih0)z1s0Rp3o69EF@q5}Z}xt{|80+YTK0Rn}-6#)XXh!X(<;g<se0+YQJ0RqjH0|5f%hZ6w;=Z6yk0<DJ=0Rokg0|5fRgcAV*pSu+S0=0t^0Rp>$69EE;ffE4&k$@8c0`s^P0Rpl60s#W=`w{^HmG=?>0-f{{0RsKB6#)Xlv=spY&+-xh0)yoO0RpkK6#)X7<N^T#iQobO0)^fJ0Rpq_5&;7LvK0XWyX+DH0<-KA0RpG&5&;60>=FS2iR=;q0>SGN0Rp+u0s';
var pinnedNfcData = null;

function decodeBase85(text) {
  var alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~';
  var values = {};
  for (var alphabetIndex = 0; alphabetIndex < alphabet.length; alphabetIndex += 1) {
    values[alphabet[alphabetIndex]] = alphabetIndex;
  }
  var output = [];
  for (var offset = 0; offset < text.length; offset += 5) {
    var chunk = text.slice(offset, offset + 5);
    var padding = 5 - chunk.length;
    chunk += '~'.repeat(padding);
    var accumulator = 0;
    for (var index = 0; index < 5; index += 1) {
      var value = values[chunk[index]];
      if (value === undefined) {
        fail();
      }
      accumulator = accumulator * 85 + value;
    }
    if (accumulator > 0xffffffff) {
      fail();
    }
    var decoded = [
      Math.floor(accumulator / 0x1000000) & 0xff,
      Math.floor(accumulator / 0x10000) & 0xff,
      Math.floor(accumulator / 0x100) & 0xff,
      accumulator & 0xff
    ];
    for (var byteIndex = 0; byteIndex < 4 - padding; byteIndex += 1) {
      output.push(decoded[byteIndex]);
    }
  }
  return output;
}

function readVarint(state) {
  var value = 0;
  var shift = 0;
  while (true) {
    if (state.position >= state.bytes.length || shift > 35) {
      fail();
    }
    var byte = state.bytes[state.position];
    state.position += 1;
    value += (byte & 0x7f) * Math.pow(2, shift);
    if (byte < 0x80) {
      return value;
    }
    shift += 7;
  }
}

function loadPinnedNfcData() {
  if (pinnedNfcData !== null) {
    return pinnedNfcData;
  }
  var bytes = decodeBase85(PINNED_NFC_DATA);
  if (bytes.length !== 13545) {
    fail();
  }
  var state = {bytes: bytes, position: 0};
  if (readVarint(state) !== 1) {
    fail();
  }
  var classCount = readVarint(state);
  var classes = new Map();
  var codepoint = 0;
  for (var classIndex = 0; classIndex < classCount; classIndex += 1) {
    codepoint += readVarint(state);
    classes.set(codepoint, readVarint(state));
  }
  var decompositionCount = readVarint(state);
  var decompositions = new Map();
  var compositions = new Map();
  var compositionCount = 0;
  codepoint = 0;
  for (var decompositionIndex = 0; decompositionIndex < decompositionCount; decompositionIndex += 1) {
    codepoint += readVarint(state);
    var lengthAndFlag = readVarint(state);
    var length = Math.floor(lengthAndFlag / 2);
    var compositionEnabled = lengthAndFlag % 2 === 1;
    var parts = [];
    for (var partIndex = 0; partIndex < length; partIndex += 1) {
      var encodedDifference = readVarint(state);
      var difference = encodedDifference % 2 === 0
        ? encodedDifference / 2
        : -Math.floor(encodedDifference / 2) - 1;
      parts.push(codepoint + difference);
    }
    decompositions.set(codepoint, parts);
    if (compositionEnabled) {
      if (parts.length !== 2) {
        fail();
      }
      compositions.set(parts[0] + ',' + parts[1], codepoint);
      compositionCount += 1;
    }
  }
  if (
    classCount !== 922 ||
    decompositionCount !== 2061 ||
    compositionCount !== 941 ||
    state.position !== bytes.length
  ) {
    fail();
  }
  pinnedNfcData = {
    classes: classes,
    compositions: compositions,
    decompositions: decompositions
  };
  return pinnedNfcData;
}

function decomposeCodepoint(codepoint, output, data) {
  var sIndex = codepoint - 0xac00;
  if (sIndex >= 0 && sIndex < 11172) {
    output.push(0x1100 + Math.floor(sIndex / 588));
    output.push(0x1161 + Math.floor((sIndex % 588) / 28));
    var trailing = sIndex % 28;
    if (trailing !== 0) {
      output.push(0x11a7 + trailing);
    }
    return;
  }
  var parts = data.decompositions.get(codepoint);
  if (parts === undefined) {
    output.push(codepoint);
    return;
  }
  for (var index = 0; index < parts.length; index += 1) {
    decomposeCodepoint(parts[index], output, data);
  }
}

function canonicalOrder(points, data) {
  var ordered = [];
  var marks = [];
  var ordinal = 0;
  function flushMarks() {
    marks.sort(function (left, right) {
      return left.combiningClass - right.combiningClass || left.ordinal - right.ordinal;
    });
    for (var markIndex = 0; markIndex < marks.length; markIndex += 1) {
      ordered.push(marks[markIndex].codepoint);
    }
    marks = [];
  }
  for (var index = 0; index < points.length; index += 1) {
    var codepoint = points[index];
    var combiningClass = data.classes.get(codepoint) || 0;
    if (combiningClass === 0) {
      flushMarks();
      ordered.push(codepoint);
    } else {
      marks.push({
        codepoint: codepoint,
        combiningClass: combiningClass,
        ordinal: ordinal
      });
      ordinal += 1;
    }
  }
  flushMarks();
  return ordered;
}

function composePair(starter, codepoint, data) {
  if (starter >= 0x1100 && starter < 0x1113 && codepoint >= 0x1161 && codepoint < 0x1176) {
    return 0xac00 + (starter - 0x1100) * 588 + (codepoint - 0x1161) * 28;
  }
  var sIndex = starter - 0xac00;
  if (
    sIndex >= 0 &&
    sIndex < 11172 &&
    sIndex % 28 === 0 &&
    codepoint > 0x11a7 &&
    codepoint < 0x11c3
  ) {
    return starter + codepoint - 0x11a7;
  }
  var composite = data.compositions.get(starter + ',' + codepoint);
  return composite === undefined ? null : composite;
}

function canonicalCompose(points, data) {
  if (points.length === 0) {
    return points;
  }
  var output = [points[0]];
  var starter = points[0];
  var starterIndex = 0;
  var lastClass = 0;
  for (var index = 1; index < points.length; index += 1) {
    var codepoint = points[index];
    var combiningClass = data.classes.get(codepoint) || 0;
    var composite = composePair(starter, codepoint, data);
    if (composite !== null && (lastClass === 0 || lastClass < combiningClass)) {
      output[starterIndex] = composite;
      starter = composite;
    } else {
      if (combiningClass === 0) {
        starter = codepoint;
        starterIndex = output.length;
      }
      output.push(codepoint);
      lastClass = combiningClass;
    }
  }
  return output;
}

function isPinnedNfc(value) {
  var ascii = true;
  for (var unitIndex = 0; unitIndex < value.length; unitIndex += 1) {
    if (value.charCodeAt(unitIndex) > 0x7f) {
      ascii = false;
      break;
    }
  }
  if (ascii) {
    return true;
  }
  var data = loadPinnedNfcData();
  var original = Array.from(value, function (character) {
    return character.codePointAt(0);
  });
  var decomposed = [];
  for (var index = 0; index < original.length; index += 1) {
    decomposeCodepoint(original[index], decomposed, data);
  }
  var normalized = canonicalCompose(canonicalOrder(decomposed, data), data);
  if (normalized.length !== original.length) {
    return false;
  }
  for (var pointIndex = 0; pointIndex < original.length; pointIndex += 1) {
    if (normalized[pointIndex] !== original[pointIndex]) {
      return false;
    }
  }
  return true;
}

function isHighSurrogate(code) {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code) {
  return code >= 0xdc00 && code <= 0xdfff;
}

function compareCodePoints(left, right) {
  var leftPoints = Array.from(left);
  var rightPoints = Array.from(right);
  var limit = Math.min(leftPoints.length, rightPoints.length);
  for (var index = 0; index < limit; index += 1) {
    var leftCode = leftPoints[index].codePointAt(0);
    var rightCode = rightPoints[index].codePointAt(0);
    if (leftCode !== rightCode) {
      return leftCode < rightCode ? -1 : 1;
    }
  }
  if (leftPoints.length === rightPoints.length) {
    return 0;
  }
  return leftPoints.length < rightPoints.length ? -1 : 1;
}

function canonicalString(value) {
  var output = '"';
  for (var index = 0; index < value.length; index += 1) {
    var code = value.charCodeAt(index);
    if (isHighSurrogate(code)) {
      if (index + 1 >= value.length || !isLowSurrogate(value.charCodeAt(index + 1))) {
        fail();
      }
      output += value.slice(index, index + 2);
      index += 1;
    } else if (isLowSurrogate(code)) {
      fail();
    } else if (code === 0x22) {
      output += '\\"';
    } else if (code === 0x5c) {
      output += '\\\\';
    } else if (code === 0x08) {
      output += '\\b';
    } else if (code === 0x0c) {
      output += '\\f';
    } else if (code === 0x0a) {
      output += '\\n';
    } else if (code === 0x0d) {
      output += '\\r';
    } else if (code === 0x09) {
      output += '\\t';
    } else if (code < 0x20) {
      output += '\\u' + code.toString(16).padStart(4, '0');
    } else {
      output += value[index];
    }
  }
  return output + '"';
}

function pythonFloat(value) {
  if (!Number.isFinite(value)) {
    return null;
  }
  if (value === 0) {
    return 1 / value < 0 ? '-0.0' : '0.0';
  }
  var sign = value < 0 ? '-' : '';
  var text = String(Math.abs(value));
  var pieces = text.toLowerCase().split('e');
  var mantissa = pieces[0];
  var declaredExponent = pieces.length === 2 ? Number(pieces[1]) : 0;
  var dot = mantissa.indexOf('.');
  var decimalPosition = dot === -1 ? mantissa.length : dot;
  var allDigits = mantissa.replace('.', '');
  var leading = allDigits.search(/[1-9]/);
  var digits = allDigits.slice(leading).replace(/0+$/, '');
  var exponent = declaredExponent + decimalPosition - leading - 1;
  if (exponent < -4 || exponent >= 16) {
    var coefficient = digits.length === 1 ? digits : digits[0] + '.' + digits.slice(1);
    var exponentSign = exponent < 0 ? '-' : '+';
    return sign + coefficient + 'e' + exponentSign + String(Math.abs(exponent)).padStart(2, '0');
  }
  var position = exponent + 1;
  if (position <= 0) {
    return sign + '0.' + '0'.repeat(-position) + digits;
  }
  if (position >= digits.length) {
    return sign + digits + '0'.repeat(position - digits.length) + '.0';
  }
  return sign + digits.slice(0, position) + '.' + digits.slice(position);
}

function Parser(text) {
  this.text = text;
  this.position = 0;
}

Parser.prototype.peek = function () {
  return this.text[this.position];
};

Parser.prototype.consume = function (expected) {
  if (this.text.slice(this.position, this.position + expected.length) !== expected) {
    fail();
  }
  this.position += expected.length;
};

Parser.prototype.parseString = function () {
  var start = this.position;
  this.consume('"');
  var decoded = '';
  while (this.position < this.text.length) {
    var code = this.text.charCodeAt(this.position);
    if (code === 0x22) {
      this.position += 1;
      if (!isPinnedNfc(decoded)) {
        fail();
      }
      if (this.text.slice(start, this.position) !== canonicalString(decoded)) {
        fail();
      }
      return decoded;
    }
    if (code === 0x5c) {
      this.position += 1;
      var escapeCode = this.peek();
      this.position += 1;
      var escapes = {
        '"': '"',
        '\\': '\\',
        '/': '/',
        b: '\b',
        f: '\f',
        n: '\n',
        r: '\r',
        t: '\t'
      };
      if (Object.prototype.hasOwnProperty.call(escapes, escapeCode)) {
        decoded += escapes[escapeCode];
        continue;
      }
      if (escapeCode !== 'u') {
        fail();
      }
      var hex = this.text.slice(this.position, this.position + 4);
      if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
        fail();
      }
      this.position += 4;
      var escapedCode = parseInt(hex, 16);
      if (isHighSurrogate(escapedCode)) {
        if (this.text.slice(this.position, this.position + 2) !== '\\u') {
          fail();
        }
        var lowHex = this.text.slice(this.position + 2, this.position + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(lowHex)) {
          fail();
        }
        var lowCode = parseInt(lowHex, 16);
        if (!isLowSurrogate(lowCode)) {
          fail();
        }
        this.position += 6;
        decoded += String.fromCodePoint(
          0x10000 + ((escapedCode - 0xd800) << 10) + (lowCode - 0xdc00)
        );
      } else if (isLowSurrogate(escapedCode)) {
        fail();
      } else {
        decoded += String.fromCharCode(escapedCode);
      }
      continue;
    }
    if (code < 0x20 || isLowSurrogate(code)) {
      fail();
    }
    if (isHighSurrogate(code)) {
      if (
        this.position + 1 >= this.text.length ||
        !isLowSurrogate(this.text.charCodeAt(this.position + 1))
      ) {
        fail();
      }
      decoded += this.text.slice(this.position, this.position + 2);
      this.position += 2;
    } else {
      decoded += this.text[this.position];
      this.position += 1;
    }
  }
  fail();
};

Parser.prototype.parseNumber = function () {
  var start = this.position;
  while (
    this.position < this.text.length &&
    this.peek() !== ',' &&
    this.peek() !== ']' &&
    this.peek() !== '}'
  ) {
    this.position += 1;
  }
  var token = this.text.slice(start, this.position);
  var match = /^(-?)(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/.exec(token);
  if (match === null) {
    fail();
  }
  var isFloat = match[3] !== undefined || match[4] !== undefined;
  if (!isFloat) {
    if (token === '-0' || match[2].length > 4300) {
      fail();
    }
    return;
  }
  var value = Number(token);
  if (!Number.isFinite(value) || pythonFloat(value) !== token) {
    fail();
  }
};

Parser.prototype.parseArray = function (depth) {
  if (depth > 500) {
    fail();
  }
  this.consume('[');
  if (this.peek() === ']') {
    this.position += 1;
    return;
  }
  while (true) {
    this.parseValue(depth);
    if (this.peek() === ']') {
      this.position += 1;
      return;
    }
    this.consume(',');
  }
};

Parser.prototype.parseObject = function (depth) {
  if (depth > 500) {
    fail();
  }
  this.consume('{');
  if (this.peek() === '}') {
    this.position += 1;
    return;
  }
  var seen = new Set();
  var previous = null;
  while (true) {
    var key = this.parseString();
    if (seen.has(key) || (previous !== null && compareCodePoints(previous, key) >= 0)) {
      fail();
    }
    seen.add(key);
    previous = key;
    this.consume(':');
    this.parseValue(depth);
    if (this.peek() === '}') {
      this.position += 1;
      return;
    }
    this.consume(',');
  }
};

Parser.prototype.parseValue = function (containerDepth) {
  var token = this.peek();
  if (token === '{') {
    this.parseObject(containerDepth + 1);
  } else if (token === '[') {
    this.parseArray(containerDepth + 1);
  } else if (token === '"') {
    this.parseString();
  } else if (token === 'n') {
    this.consume('null');
  } else if (token === 't') {
    this.consume('true');
  } else if (token === 'f') {
    this.consume('false');
  } else if (token === '-' || (token >= '0' && token <= '9')) {
    this.parseNumber();
  } else {
    fail();
  }
};

try {
  if (typeof raw !== 'string' || raw.length === 0 || raw[0] !== '{') {
    return false;
  }
  var parser = new Parser(raw);
  parser.parseObject(1);
  return parser.position === raw.length;
} catch (error) {
  return false;
}
""";
