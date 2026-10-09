set terminal pngcairo size 1400,850 enhanced font "Arial,16" background rgb "#11151a"
set output "previews/antialiasing-histogram.png"
set datafile separator comma
set border 3 lc rgb "#8492a6"
set tics textcolor rgb "#c9d1dc"
set xtics 16
set ytics 1000
set grid ytics lc rgb "#303b48"
set xrange [0:255]
set yrange [0:5400]
set xlabel "Gray value (sRGB, 0–255)" textcolor rgb "#c9d1dc"
set ylabel "Pixel count" textcolor rgb "#c9d1dc"
set title "RED2 anti-aliasing: intermediate gray distribution\nFull specimen · 13,515 pixels · 26 shades · pure black and white excluded" textcolor rgb "#ffffff" font "Arial,21"
set boxwidth 3 absolute
set style fill solid 1.0 noborder
set key outside bottom center horizontal maxrows 1 textcolor rgb "#c9d1dc" font "Arial,12" spacing 1.2
set arrow 1 from 78,0 to 78,5000 nohead dt 2 lw 2 lc rgb "#c49bff"
set arrow 2 from 162,0 to 162,5000 nohead dt 2 lw 2 lc rgb "#c49bff"
set arrow 3 from 59,0 to 59,4100 nohead dt 3 lw 2 lc rgb "#57dfbb"
set arrow 4 from 198,0 to 198,4100 nohead dt 3 lw 2 lc rgb "#57dfbb"
set label 1 "78" at 78,5200 center textcolor rgb "#c49bff"
set label 2 "162" at 162,5200 center textcolor rgb "#c49bff"
set label 3 "dark mean ≈ 59" at 59,4350 center textcolor rgb "#57dfbb"
set label 4 "light mean ≈ 198" at 220,4350 center textcolor rgb "#57dfbb"
set label 5 "191: 4,857 pixels (35.9%)" at 191,5020 center textcolor rgb "#ffffff"
set label 6 "64: 2,299" at 82,2600 left textcolor rgb "#ffffff"
set label 7 "60: 1,981" at 41,2180 right textcolor rgb "#ffffff"
plot "previews/antialiasing-histogram.csv" every ::1 using 1:2 with boxes lc rgb "#83b8ee" title "Intermediate pixels", \
     NaN with lines dt 2 lw 2 lc rgb "#c49bff" title "Current perceptual grays: 78, 162", \
     NaN with lines dt 3 lw 2 lc rgb "#57dfbb" title "Mean within dark / light groups"
