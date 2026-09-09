function startVoice() {

    if ("speechSynthesis" in window) {

        const speech = new SpeechSynthesisUtterance(
            "Welcome to AgriVision. Your smart farming assistant."
        );

        speech.lang = "en-IN";

        window.speechSynthesis.speak(speech);

    } else {

        alert("Voice support is not available.");

    }
}


function changeLanguage() {

    alert("Telugu and additional languages will be added soon.");

}


/* ================= SCREEN CONTROL ================= */

function hideAllScreens() {

    const screens = document.querySelectorAll(".screen");

    screens.forEach(function(screen) {

        screen.style.display = "none";

    });

}


function hideHome() {

    document.querySelector(".welcome").style.display = "none";

    document.querySelector(".features").style.display = "none";

    document.querySelector(".quick-info").style.display = "none";

}


function goHome() {

    hideAllScreens();

    document.querySelector(".welcome").style.display = "block";

    document.querySelector(".features").style.display = "block";

    document.querySelector(".quick-info").style.display = "block";

}


/* ================= CROP ================= */

function openFeature(feature) {

    if (feature === "crop") {

        hideHome();
        hideAllScreens();

        document.getElementById("cropScreen").style.display = "block";

    }

    else if (feature === "weather") {

        hideHome();
        hideAllScreens();

        document.getElementById("weatherScreen").style.display = "block";

    }

    else if (feature === "ndvi") {

        hideHome();
        hideAllScreens();

        document.getElementById("ndviScreen").style.display = "block";

    }

    else if (feature === "doctor") {

        hideHome();
        hideAllScreens();

        document.getElementById("doctorScreen").style.display = "block";

    }

    else if (feature === "calendar") {

        hideHome();
        hideAllScreens();

        document.getElementById("calendarScreen").style.display = "block";

    }

    else if (feature === "guidance") {

        hideHome();
        hideAllScreens();

        document.getElementById("guidanceScreen").style.display = "block";

    }

}
/* ================= IRRIGATION ================= */
async function openIrrigation() {

    hideAllScreens();

    document.getElementById("irrigationScreen").style.display =
        "block";

    const crop = getSelectedCrop();

    document.getElementById("irrigationCropName").innerText =
        "Checking live weather for " + crop + "...";

    document.getElementById("waterFrequency").innerText =
        "Getting live data...";

    document.getElementById("waterAmount").innerText =
        "Getting live data...";


    if (!navigator.geolocation) {

        document.getElementById("waterFrequency").innerText =
            "Location unavailable.";

        document.getElementById("waterAmount").innerText =
            "Please enable location.";

        return;
    }


    navigator.geolocation.getCurrentPosition(

        async function(position) {

            const latitude =
                position.coords.latitude;

            const longitude =
                position.coords.longitude;


            const url =
                "https://api.open-meteo.com/v1/forecast" +

                "?latitude=" + latitude +

                "&longitude=" + longitude +

                "&current=" +

                "temperature_2m," +
                "relative_humidity_2m," +
                "precipitation," +
                "rain," +
                "weather_code" +

                "&hourly=" +

                "precipitation_probability," +
                "soil_moisture_0_to_1cm" +

                "&timezone=auto";


            try {

                const response =
                    await fetch(url);


                const data =
                    await response.json();


                const current =
                    data.current;

                const hourly =
                    data.hourly;


                const rain =
                    Number(current.rain || 0);


                const precipitation =
                    Number(current.precipitation || 0);


                const rainChance =
                    Number(
                        hourly.precipitation_probability[0] || 0
                    );


                const soilMoisture =
                    Number(
                        hourly.soil_moisture_0_to_1cm[0] || 0
                    );


                const temperature =
                    Number(current.temperature_2m);


                /*
                =========================================
                IRRIGATION DECISION
                =========================================
                */


                let recommendation = "";

                let amount = "";


                if (
                    rain > 0 ||
                    precipitation > 0 ||
                    rainChance >= 60
                ) {

                    recommendation =
                        "⚠️ Irrigation may not be needed now because rainfall is occurring or likely.";

                    amount =
                        "Avoid unnecessary watering and reassess after rainfall.";

                }

                else if (
                    soilMoisture < 0.20
                ) {

                    recommendation =
                        "💧 Soil moisture is relatively low. Irrigation may be required.";

                    amount =
                        "Provide moderate irrigation according to crop stage and soil condition.";

                }

                else if (
                    soilMoisture < 0.30
                ) {

                    recommendation =
                        "🌱 Soil moisture is moderate. Monitor before irrigating.";

                    amount =
                        "Light irrigation may be considered if the crop shows water stress.";

                }

                else {

                    recommendation =
                        "✅ Soil moisture is currently adequate.";

                    amount =
                        "Irrigation may not be necessary immediately.";

                }


                /*
                =========================================
                CROP-SPECIFIC MESSAGE
                =========================================
                */


                let cropMessage = "";


                if (crop === "Jasmine") {

                    cropMessage =
                        "Jasmine: avoid prolonged waterlogging and monitor soil moisture.";

                }

                else if (crop === "Paddy") {

                    cropMessage =
                        "Paddy: irrigation decisions should consider the crop growth stage and field water condition.";

                }

                else if (crop === "Groundnut") {

                    cropMessage =
                        "Groundnut: avoid both prolonged dryness and excessive moisture.";

                }

                else if (crop === "Chilli") {

                    cropMessage =
                        "Chilli: maintain suitable soil moisture and avoid excess irrigation.";

                }

                else if (crop === "Onion") {

                    cropMessage =
                        "Onion: maintain consistent soil moisture while avoiding waterlogging.";

                }


                document.getElementById(
                    "irrigationCropName"
                ).innerText =
                    "🌦️ Live irrigation analysis for " + crop;


                document.getElementById(
                    "waterFrequency"
                ).innerText =
                    recommendation;


                document.getElementById(
                    "waterAmount"
                ).innerText =
                    amount;


                /*
                Add extra live information
                */


                const moduleBox =
                    document.querySelector(
                        "#irrigationScreen .module-box"
                    );


                let liveInfo =
                    document.getElementById(
                        "liveIrrigationInfo"
                    );


                if (!liveInfo) {

                    liveInfo =
                        document.createElement("div");

                    liveInfo.id =
                        "liveIrrigationInfo";

                    liveInfo.style.marginTop =
                        "25px";

                    liveInfo.style.padding =
                        "20px";

                    liveInfo.style.background =
                        "#f4f8f2";

                    liveInfo.style.borderRadius =
                        "15px";

                    moduleBox.appendChild(
                        liveInfo
                    );

                }


                liveInfo.innerHTML =

                    "<strong>🌦️ Live Conditions</strong><br><br>" +

                    "🌡️ Temperature: " +
                    temperature +
                    "°C<br>" +

                    "🌧️ Rain probability: " +
                    rainChance +
                    "%<br>" +

                    "💧 Soil moisture: " +
                    soilMoisture.toFixed(2) +
                    " m³/m³<br><br>" +

                    "<strong>🌱 Crop advice</strong><br>" +
                    cropMessage;


            }

            catch (error) {

                console.error(error);

                document.getElementById(
                    "irrigationCropName"
                ).innerText =
                    "Unable to get live weather.";


                document.getElementById(
                    "waterFrequency"
                ).innerText =
                    "Check your internet connection.";


                document.getElementById(
                    "waterAmount"
                ).innerText =
                    "Try again later.";

            }

        },


        function() {

            document.getElementById(
                "irrigationCropName"
            ).innerText =
                "📍 Please allow location access.";

            document.getElementById(
                "waterFrequency"
            ).innerText =
                "Location permission is required.";

            document.getElementById(
                "waterAmount"
            ).innerText =
                "Enable location and try again.";

        },


        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 300000
        }

    );

}

/* ================= FERTILIZER ================= */

function openFertilizer() {

    hideAllScreens();

    document.getElementById("fertilizerScreen").style.display = "block";

    document.getElementById("fertilizerCropName").innerText =
        "Fertilizer guidance for " + getSelectedCrop();

}


/* ================= PEST ================= */

function openPest() {

    hideAllScreens();

    document.getElementById("pestScreen").style.display = "block";

    document.getElementById("pestCropName").innerText =
        "Pest management guidance for " + getSelectedCrop();

}


/* ================= CALENDAR ================= */

function openCalendar() {

    hideAllScreens();

    document.getElementById("calendarScreen").style.display = "block";

    document.getElementById("calendarCropName").innerText =
        "Farming activities for " + getSelectedCrop();

}


/* ================= VOICE GUIDANCE ================= */

function openVoiceGuidance() {

    hideAllScreens();

    document.getElementById("voiceScreen").style.display = "block";

    document.getElementById("voiceCropName").innerText =
        "Voice guidance for " + getSelectedCrop();

}


function speakCropGuidance() {

    const crop = getSelectedCrop();

    const speech = new SpeechSynthesisUtterance(
        "Welcome to AgriVision. Here is your farming guidance for " +
        crop +
        ". Monitor your crop regularly. Maintain proper irrigation. Apply fertilizers according to crop requirements. Check regularly for pests and diseases."
    );

    speech.lang = "en-IN";

    window.speechSynthesis.speak(speech);

}


/* ================= WEATHER ================= */

/* ================= LIVE WEATHER ================= */

function openWeather() {

    hideHome();

    hideAllScreens();

    document.getElementById("weatherScreen").style.display =
        "block";

    getLiveWeather();

loadSevenDayForecast(
    latitude,
    longitude
);
}


function getLiveWeather() {

    document.getElementById("weatherStatus").innerText =
        "📍 Requesting your location...";

    if (!navigator.geolocation) {

        document.getElementById("weatherStatus").innerText =
            "Location is not supported by this browser.";

        return;

    }


    navigator.geolocation.getCurrentPosition(

        function(position) {

            const latitude =
                position.coords.latitude;

            const longitude =
                position.coords.longitude;

            fetchLiveWeather(
                latitude,
                longitude
            );

        },

        function(error) {

            console.error(error);

            document.getElementById("weatherStatus").innerText =
                "⚠️ Please allow location access to get live weather.";

        },

        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 300000
        }

    );

}


async function fetchLiveWeather(
    latitude,
    longitude
) {

    document.getElementById("weatherStatus").innerText =
        "🌦️ Getting live weather...";


    const url =
        "https://api.open-meteo.com/v1/forecast" +

        "?latitude=" + latitude +

        "&longitude=" + longitude +

        "&current=" +

        "temperature_2m," +
        "relative_humidity_2m," +
        "apparent_temperature," +
        "precipitation," +
        "rain," +
        "weather_code," +
        "wind_speed_10m" +

        "&hourly=" +

        "precipitation_probability," +
        "soil_moisture_0_to_1cm" +

        "&timezone=auto";


    try {

        const response =
            await fetch(url);


        if (!response.ok) {

            throw new Error(
                "Weather request failed"
            );

        }


        const data =
            await response.json();


        const current =
            data.current;


        const hourly =
            data.hourly;


        /* Temperature */

        document.getElementById(
            "temperature"
        ).innerText =
            Math.round(
                current.temperature_2m
            ) + "°C";


        /* Feels Like */

        document.getElementById(
            "feelsLike"
        ).innerText =
            Math.round(
                current.apparent_temperature
            ) + "°C";


        /* Humidity */

        document.getElementById(
            "humidity"
        ).innerText =
            current.relative_humidity_2m + "%";


        /* Wind */

        document.getElementById(
            "wind"
        ).innerText =
            Math.round(
                current.wind_speed_10m
            ) + " km/h";


        /* Rain */

        document.getElementById(
            "rainAmount"
        ).innerText =
            current.precipitation + " mm";


        /* Rain probability */

        if (
            hourly &&
            hourly.precipitation_probability
        ) {

            document.getElementById(
                "rainChance"
            ).innerText =
                hourly.precipitation_probability[0] +
                "%";

        }


        /* Soil moisture */

        if (
            hourly &&
            hourly.soil_moisture_0_to_1cm
        ) {

            const moisture =
                hourly.soil_moisture_0_to_1cm[0];

            document.getElementById(
                "soilMoisture"
            ).innerText =
                moisture.toFixed(2) +
                " m³/m³";

        }


        /* Weather condition */

        const condition =
            getWeatherDescription(
                current.weather_code
            );


        document.getElementById(
            "weatherCondition"
        ).innerText =
            condition;


        /* Weather icon */

        document.getElementById(
            "weatherIcon"
        ).innerText =
            getWeatherIcon(
                current.weather_code
            );


        /* Location */

        document.getElementById(
            "weatherLocation"
        ).innerText =
            "Current Location";


        document.getElementById(
            "weatherStatus"
        ).innerText =
            "✅ Live weather updated successfully.";

    }

    catch (error) {

        console.error(error);

        document.getElementById(
            "weatherStatus"
        ).innerText =
            "❌ Unable to load live weather. Check your internet connection.";

    }

}


/* ================= WEATHER CODES ================= */

function getWeatherDescription(code) {

    if (code === 0) {
        return "Clear Sky";
    }

    if (code === 1 ||
        code === 2) {
        return "Partly Cloudy";
    }

    if (code === 3) {
        return "Cloudy";
    }

    if (
        code === 45 ||
        code === 48
    ) {
        return "Foggy";
    }

    if (
        code >= 51 &&
        code <= 57
    ) {
        return "Drizzle";
    }

    if (
        code >= 61 &&
        code <= 67
    ) {
        return "Rain";
    }

    if (
        code >= 71 &&
        code <= 77
    ) {
        return "Snow";
    }

    if (
        code >= 80 &&
        code <= 82
    ) {
        return "Rain Showers";
    }

    if (
        code >= 95
    ) {
        return "Thunderstorm";
    }

    return "Mixed Weather";

}


function getWeatherIcon(code) {

    if (code === 0) {
        return "☀️";
    }

    if (
        code === 1 ||
        code === 2
    ) {
        return "⛅";
    }

    if (code === 3) {
        return "☁️";
    }

    if (
        code >= 51 &&
        code <= 67
    ) {
        return "🌧️";
    }

    if (
        code >= 80 &&
        code <= 82
    ) {
        return "🌦️";
    }

    if (code >= 95) {
        return "⛈️";
    }

    return "🌤️";

}
/* ================= CROP DOCTOR ================= */

function openDoctor() {

    hideHome();

    hideAllScreens();

    document.getElementById("doctorScreen").style.display = "block";

}


function previewCropImage(event) {

    const file = event.target.files[0];

    if (!file) {
        return;
    }

    const reader = new FileReader();

    reader.onload = function(event) {

        document.getElementById("imagePreview").innerHTML =
            "<img src='" + event.target.result + "'>";

    };

    reader.readAsDataURL(file);

}


function analyzeCrop() {

    const file =
        document.getElementById("cropImage").files[0];

    if (!file) {

        alert("Please upload a crop image first.");

        return;

    }

    document.getElementById("doctorResult").innerHTML =

        "<strong>🔍 Image Received</strong><br><br>" +

        "Your crop image has been uploaded successfully.<br><br>" +

        "🤖 AI disease detection will be connected in the next stage.";

}


/* ================= NDVI ================= */

function openNDVI() {

    hideHome();

    hideAllScreens();

    document.getElementById("ndviScreen").style.display = "block";

}


/* ================= GIS ================= */

function openMap() {

    hideHome();

    hideAllScreens();

    document.getElementById("mapScreen").style.display = "block";

}
/* ================= 7 DAY FORECAST ================= */

async function loadSevenDayForecast(
    latitude,
    longitude
) {

    const forecastContainer =
        document.getElementById(
            "forecastContainer"
        );

    forecastContainer.innerHTML =
        "<p>Loading 7-day forecast...</p>";


    const url =
        "https://api.open-meteo.com/v1/forecast" +

        "?latitude=" + latitude +

        "&longitude=" + longitude +

        "&daily=" +

        "weather_code," +
        "temperature_2m_max," +
        "temperature_2m_min," +
        "precipitation_probability_max" +

        "&timezone=auto";


    try {

        const response =
            await fetch(url);


        if (!response.ok) {

            throw new Error(
                "Forecast request failed"
            );

        }


        const data =
            await response.json();


        const daily =
            data.daily;


        forecastContainer.innerHTML = "";


        for (
            let i = 0;
            i < daily.time.length;
            i++
        ) {

            const date =
                new Date(
                    daily.time[i] +
                    "T00:00:00"
                );


            const day =
                date.toLocaleDateString(
                    "en-IN",
                    {
                        weekday: "short"
                    }
                );


            const maxTemp =
                Math.round(
                    daily.temperature_2m_max[i]
                );


            const minTemp =
                Math.round(
                    daily.temperature_2m_min[i]
                );


            const rainChance =
                daily.precipitation_probability_max[i];


            const icon =
                getWeatherIcon(
                    daily.weather_code[i]
                );


            const card =
                document.createElement("div");


            card.className =
                "forecast-card";


            card.innerHTML =

                "<div class='forecast-day'>" +
                day +
                "</div>" +

                "<div class='forecast-icon'>" +
                icon +
                "</div>" +

                "<div class='forecast-temp'>" +
                maxTemp +
                "° / " +
                minTemp +
                "°C" +
                "</div>" +

                "<div class='forecast-rain'>" +
                "🌧️ " +
                rainChance +
                "% rain" +
                "</div>";


            forecastContainer.appendChild(
                card
            );

        }

    }

    catch (error) {

        console.error(error);

        forecastContainer.innerHTML =
            "<p>Unable to load forecast.</p>";

    }

}
/* ================= NDVI LOCATION ================= */

/* ================= REAL NDVI ANALYSIS ================= */

async function getNDVILocation() {

    const status =
        document.getElementById("ndviLocationStatus");

    status.innerText =
        "📍 Detecting your farm location...";

    if (!navigator.geolocation) {

        status.innerText =
            "❌ Location is not supported by this browser.";

        return;
    }

    try {

        const position = await new Promise(
            (resolve, reject) => {

                navigator.geolocation.getCurrentPosition(
                    resolve,
                    reject,
                    {
                        enableHighAccuracy: true,
                        timeout: 10000,
                        maximumAge: 300000
                    }
                );

            }
        );

        const latitude =
            position.coords.latitude;

        const longitude =
            position.coords.longitude;

        status.innerText =
            "📍 Location detected. Analyzing satellite data...";

        const response = await fetch(
            "http://127.0.0.1:5000/api/ndvi" +
            "?lat=" + latitude +
            "&lon=" + longitude
        );

        const data =
            await response.json();

        if (!data.success) {

            status.innerText =
                "❌ " +
                (data.message ||
                "NDVI analysis failed.");

            return;
        }

        status.innerText =
            "✅ Farm location: " +
            latitude.toFixed(5) +
            ", " +
            longitude.toFixed(5);


        let resultBox =
            document.getElementById(
                "realNdviResult"
            );


        if (!resultBox) {

            resultBox =
                document.createElement("div");

            resultBox.id =
                "realNdviResult";

            resultBox.style.margin =
                "30px auto";

            resultBox.style.padding =
                "25px";

            resultBox.style.maxWidth =
                "850px";

            resultBox.style.borderRadius =
                "18px";

            resultBox.style.background =
                "#f4f8f2";

            resultBox.style.textAlign =
                "center";

            status.parentElement
                .appendChild(resultBox);
        }


        let advice = "";

        if (data.level === "very-low") {

            advice =
                "⚠️ Very low vegetation activity. Check crop condition, moisture and possible stress.";

        } else if (data.level === "low") {

            advice =
                "🟠 Low vegetation activity. Monitor irrigation, soil condition and crop stage.";

        } else if (data.level === "moderate") {

            advice =
                "🟡 Moderate vegetation activity. Continue monitoring crop growth.";

        } else if (data.level === "high") {

            advice =
                "🟢 Good vegetation activity. Continue normal crop monitoring.";

        } else if (data.level === "very-high") {

            advice =
                "🟢 Very high vegetation activity. Crop vegetation appears strong.";
        }


        resultBox.innerHTML = `

            <h3>
                🛰️ Real Sentinel-2 NDVI
            </h3>

            <div style="
                font-size:48px;
                font-weight:bold;
                margin:15px 0;
            ">
                ${data.ndvi}
            </div>

            <p>
                🌱 <strong>Crop Health:</strong>
                ${data.health}
            </p>

            <p>
                🛰️ <strong>Sentinel-2 images analyzed:</strong>
                ${data.images_found}
            </p>

            <p>
                📍 <strong>Latitude:</strong>
                ${data.latitude.toFixed(5)}
                <br>

                📍 <strong>Longitude:</strong>
                ${data.longitude.toFixed(5)}
            </p>

            <p style="
                margin-top:20px;
            ">
                ${advice}
            </p>

        `;


        if (data.map_url) {

            resultBox.innerHTML += `

                <div class="ndvi-map-section">

                    <h3 class="ndvi-map-title">
                        🛰️ NDVI Satellite Map
                    </h3>

                    <p class="ndvi-map-description">
                        Vegetation health around your selected farm location
                    </p>

                    <div class="ndvi-map-wrapper">

                        <img
                            src="${data.map_url}"
                            alt="Real Sentinel-2 NDVI Map"
                            class="ndvi-map-image"
                        >

                        <div
                            class="farm-marker"
                            title="Selected farm location"
                        >
                            📍
                        </div>

                    </div>


                    <div class="ndvi-legend">

                        <div class="legend-title">
                            NDVI Vegetation Health
                        </div>

                        <div class="legend-items">

                            <div class="legend-item">
                                <span class="legend-color very-low"></span>
                                <span>
                                    Very Low
                                    <small>0.00 – 0.20</small>
                                </span>
                            </div>

                            <div class="legend-item">
                                <span class="legend-color low"></span>
                                <span>
                                    Low
                                    <small>0.21 – 0.35</small>
                                </span>
                            </div>

                            <div class="legend-item">
                                <span class="legend-color moderate"></span>
                                <span>
                                    Moderate
                                    <small>0.36 – 0.50</small>
                                </span>
                            </div>

                            <div class="legend-item">
                                <span class="legend-color high"></span>
                                <span>
                                    High
                                    <small>0.51 – 0.70</small>
                                </span>
                            </div>

                            <div class="legend-item">
                                <span class="legend-color very-high"></span>
                                <span>
                                    Very High
                                    <small>0.71 – 1.00</small>
                                </span>
                            </div>

                        </div>

                    </div>


                    <div class="ndvi-map-info">

                        <div>
                            📍 <strong>Farm:</strong>
                            ${data.latitude.toFixed(5)},
                            ${data.longitude.toFixed(5)}
                        </div>

                        <div>
                            🛰️ <strong>Satellite:</strong>
                            Sentinel-2
                        </div>

                        <div>
                            📊 <strong>Images analyzed:</strong>
                            ${data.images_found}
                        </div>

                    </div>

                </div>

            `;
        }
        // =====================================================
// INTERACTIVE NDVI MAP
// =====================================================

if (data.tile_url && typeof L !== "undefined") {

    resultBox.innerHTML += `

        <div style="
            margin-top:35px;
            width:100%;
        ">

            <h3 style="
                color:#1b5e20;
                margin-bottom:8px;
            ">
                🗺️ Interactive NDVI Map
            </h3>

            <p style="
                color:#666;
                margin-bottom:15px;
            ">
                Explore the real Sentinel-2 NDVI layer.
            </p>

            <div
                id="ndviInteractiveMap"
                style="
                    width:100%;
                    height:500px;
                    border-radius:18px;
                    overflow:hidden;
                    border:1px solid #ddd;
                "
            ></div>

        </div>

    `;
    // =====================================================
// FARM BOUNDARY CONTROL
// =====================================================

resultBox.innerHTML += `

    <div style="
        margin-top:20px;
        text-align:center;
    ">

        <button
            id="drawFarmButton"
            type="button"
            style="
                padding:12px 22px;
                border:none;
                border-radius:25px;
                background:#2e7d32;
                color:white;
                font-size:16px;
                cursor:pointer;
            "
        >
            ✏️ Draw My Farm Boundary
        </button>

        <p
            id="boundaryStatus"
            style="
                margin-top:10px;
                color:#666;
            "
        >
            Select the button to mark your farm area.
        </p>

    </div>

`;


    // Create map
    const interactiveMap =
        L.map("ndviInteractiveMap")
        .setView(
            [
                data.latitude,
                data.longitude
            ],
            17
        );


    // OpenStreetMap base layer
    L.tileLayer(
        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom:19,
            attribution:
                "&copy; OpenStreetMap contributors"
        }
    ).addTo(interactiveMap);


    // Real Earth Engine NDVI layer
    L.tileLayer(
        data.tile_url,
        {
            opacity:0.75,
            attribution:
                "NDVI © Google Earth Engine / Sentinel-2"
        }
    ).addTo(interactiveMap);


    // Farm marker
    L.marker([
        data.latitude,
        data.longitude
    ])
    .addTo(interactiveMap)
    .bindPopup(
        "<b>🌱 Your Farm</b><br>" +
        "NDVI: " + data.ndvi +
        "<br>Health: " + data.health
    )
    .openPopup();


    // 500 metre analysis circle
    L.circle(
        [
            data.latitude,
            data.longitude
        ],
        {
            radius:500,
            color:"#2e7d32",
            weight:2,
            fill:false
        }
    ).addTo(interactiveMap);


    // Map legend
    const legend =
        L.control({
            position:"bottomright"
        });


    legend.onAdd =
        function () {

            const div =
                L.DomUtil.create(
                    "div"
                );

            div.style.background =
                "white";

            div.style.padding =
                "12px";

            div.style.borderRadius =
                "10px";

            div.style.lineHeight =
                "22px";

            div.style.boxShadow =
                "0 2px 8px rgba(0,0,0,0.2)";


            div.innerHTML = `

                <strong>NDVI Health</strong><br>

                <span style="color:#8B0000">
                    ■
                </span>
                Very Low<br>

                <span style="color:#FF4500">
                    ■
                </span>
                Low<br>

                <span style="color:#FFD700">
                    ■
                </span>
                Moderate<br>

                <span style="color:#90EE90">
                    ■
                </span>
                High<br>

                <span style="color:#006400">
                    ■
                </span>
                Very High

            `;

            return div;
        };


    legend.addTo(interactiveMap);
    // =====================================================
// DRAW FARM BOUNDARY
// =====================================================

const farmLayers = new L.FeatureGroup();

interactiveMap.addLayer(farmLayers);


const drawControl = new L.Control.Draw({

    position: "topright",

    draw: {

        polygon: {

            allowIntersection: false,

            showArea: true,

            shapeOptions: {

                color: "#2e7d32",

                weight: 3,

                fillOpacity: 0.2

            }

        },

        rectangle: false,

        circle: false,

        circlemarker: false,

        marker: false,

        polyline: false

    },

    edit: {

        featureGroup: farmLayers,

        remove: true

    }

});


interactiveMap.addControl(drawControl);


// =====================================================
// DRAWING COMPLETED
// =====================================================

interactiveMap.on(
    L.Draw.Event.CREATED,
    function (event) {

        // Remove previous boundary
        farmLayers.clearLayers();

        // Add new boundary
        const layer = event.layer;

        farmLayers.addLayer(layer);
        // =====================================================
// SEND FARM BOUNDARY TO FLASK
// =====================================================

const boundaryPoints =
    layer.getLatLngs()[0].map(
        function (point) {

            return [
                point.lat,
                point.lng
            ];

        }
    );


const boundaryStatus =
    document.getElementById(
        "boundaryStatus"
    );


if (boundaryStatus) {

    boundaryStatus.innerHTML =
        "⏳ Analyzing your exact farm boundary...";

}


fetch(
    "http://127.0.0.1:5000/api/ndvi/boundary",
    {

        method: "POST",

        headers: {

            "Content-Type":
                "application/json"

        },

        body: JSON.stringify({

            coordinates:
                boundaryPoints

        })

    }
)

.then(
    response =>
        response.json()
)

.then(
    data => {

        if (!data.success) {

            if (boundaryStatus) {

                boundaryStatus.innerHTML =
                    "❌ " +
                    data.message;

            }

            return;

        }


        // =================================================
        // SHOW FARM-SPECIFIC RESULT
        // =================================================

       if (boundaryStatus) {

    const ndvi =
        Number(data.ndvi);

    let classification =
        "No Data";

    let emoji =
        "⚪";

    let status =
        "Unable to classify";

    if (ndvi <= 0.20) {

        classification =
            "Very Low";

        emoji =
            "🔴";

        status =
            "🚨 High vegetation stress";

    } else if (ndvi <= 0.35) {

        classification =
            "Low";

        emoji =
            "🟠";

        status =
            "⚠️ Needs attention";

    } else if (ndvi <= 0.50) {

        classification =
            "Moderate";

        emoji =
            "🟡";

        status =
            "🔎 Monitor regularly";

    } else if (ndvi <= 0.70) {

        classification =
            "High";

        emoji =
            "🟢";

        status =
            "✅ Healthy vegetation";

    } else {

        classification =
            "Very High";

        emoji =
            "🟢";

        status =
            "🌿 Very healthy vegetation";
    }


    boundaryStatus.innerHTML = `

        <div style="
            padding:22px;
            border-radius:16px;
            background:#f8fff6;
            border:2px solid #2e7d32;
            margin-top:15px;
        ">

            <h2 style="
                color:#1b5e20;
                margin-top:0;
            ">
                🌱 Selected Farm Vegetation Health
            </h2>

            <div style="
                text-align:center;
                padding:18px;
                margin:15px 0;
                border-radius:14px;
                background:#ffffff;
            ">

                <div style="
                    font-size:16px;
                    color:#555;
                ">
                    Mean NDVI
                </div>

                <div style="
                    font-size:52px;
                    font-weight:bold;
                    color:#2e7d32;
                ">
                    ${ndvi.toFixed(3)}
                </div>

                <div style="
                    font-size:26px;
                    font-weight:bold;
                    margin-top:5px;
                ">
                    ${emoji} ${classification}
                </div>

                <div style="
                    font-size:17px;
                    margin-top:8px;
                ">
                    ${status}
                </div>

            </div>


            <div style="
                display:grid;
                grid-template-columns:
                    repeat(auto-fit,minmax(180px,1fr));
                gap:12px;
            ">

                <div style="
                    padding:15px;
                    background:#ffffff;
                    border-radius:10px;
                    text-align:center;
                ">

                    <div>📐</div>

                    <strong>
                        Farm Area
                    </strong>

                    <br>

                    ${Number(
                        data.area_hectares
                    ).toFixed(3)}
                    hectares

                </div>


                <div style="
                    padding:15px;
                    background:#ffffff;
                    border-radius:10px;
                    text-align:center;
                ">

                    <div>🛰️</div>

                    <strong>
                        Sentinel-2 Images
                    </strong>

                    <br>

                    ${data.images_found}

                </div>

            </div>


            <div style="
                margin-top:20px;
                padding:18px;
                background:#ffffff;
                border-radius:12px;
            ">

                <h3 style="
                    color:#1b5e20;
                    margin-top:0;
                ">
                    🌈 NDVI Classification
                </h3>

                <div>
                    🔴 <strong>Very Low</strong>
                    &nbsp; 0.00 – 0.20
                </div>

                <div style="
                    margin-top:8px;
                    font-weight:bold;
                    ${
                        classification === "Low"
                        ? "background:#fff3e0;"
                        : ""
                    }
                    padding:6px;
                    border-radius:6px;
                ">
                    🟠 <strong>Low</strong>
                    &nbsp; 0.21 – 0.35
                    ${
                        classification === "Low"
                        ? " ← YOUR FARM"
                        : ""
                    }
                </div>

                <div style="
                    margin-top:8px;
                    font-weight:bold;
                    ${
                        classification === "Moderate"
                        ? "background:#fffde7;"
                        : ""
                    }
                    padding:6px;
                    border-radius:6px;
                ">
                    🟡 <strong>Moderate</strong>
                    &nbsp; 0.36 – 0.50
                    ${
                        classification === "Moderate"
                        ? " ← YOUR FARM"
                        : ""
                    }
                </div>

                <div style="
                    margin-top:8px;
                    font-weight:bold;
                    ${
                        classification === "High"
                        ? "background:#e8f5e9;"
                        : ""
                    }
                    padding:6px;
                    border-radius:6px;
                ">
                    🟢 <strong>High</strong>
                    &nbsp; 0.51 – 0.70
                    ${
                        classification === "High"
                        ? " ← YOUR FARM"
                        : ""
                    }
                </div>

                <div style="
                    margin-top:8px;
                    font-weight:bold;
                    ${
                        classification === "Very High"
                        ? "background:#e8f5e9;"
                        : ""
                    }
                    padding:6px;
                    border-radius:6px;
                ">
                    🟢 <strong>Very High</strong>
                    &nbsp; 0.71 – 1.00
                    ${
                        classification === "Very High"
                        ? " ← YOUR FARM"
                        : ""
                    }
                </div>

            </div>


            <div style="
                margin-top:18px;
                padding:14px;
                background:#eef7ed;
                border-radius:10px;
                font-size:14px;
            ">

                📌 Classification is calculated
                from the mean Sentinel-2 NDVI
                inside your selected farm boundary.

            </div>

        </div>

    `;
}
        console.log(
            "🌱 Farm-specific NDVI:",
            data.ndvi
        );

        console.log(
            "📐 Farm area:",
            data.area_hectares,
            "hectares"
        );


        // =================================================
        // SHOW FARM NDVI MAP
        // =================================================

        const farmResult =
            document.createElement("div");


        farmResult.style.marginTop =
            "20px";


        farmResult.innerHTML = `

            <div style="
                padding:20px;
                border-radius:15px;
                background:#ffffff;
                border:1px solid #ddd;
            ">

                <h3 style="
                    color:#1b5e20;
                ">
                    🌱 Exact Farm NDVI
                </h3>

                <div style="
                    font-size:40px;
                    font-weight:bold;
                    margin:10px;
                ">
                    ${data.ndvi}
                </div>

                <p>
                    Crop Health:
                    <strong>
                        ${data.health}
                    </strong>
                </p>

                <p>
                    Farm Area:
                    <strong>
                        ${data.area_hectares}
                        hectares
                    </strong>
                </p>

                <img
                    src="${data.map_url}"
                    alt="Farm-specific NDVI map"
                    style="
                        width:100%;
                        max-width:800px;
                        border-radius:15px;
                        margin-top:15px;
                    "
                >

            </div>

        `;


        resultBox.appendChild(
            farmResult
        );

    }
)

.catch(
    error => {

        console.error(
            "Farm boundary error:",
            error
        );


        if (boundaryStatus) {

            boundaryStatus.innerHTML =
                "❌ Could not connect to the NDVI server.";

        }

    }
);


        // Calculate approximate area
        const area =
            L.GeometryUtil.geodesicArea(
                layer.getLatLngs()[0]
            );

        const areaHectares =
            area / 10000;


        // Update status
        const status =
            document.getElementById(
                "boundaryStatus"
            );


        if (status) {

            status.innerHTML =

                "✅ Farm boundary selected<br>" +

                "📐 Area: " +

                areaHectares.toFixed(2) +

                " hectares";

        }


        console.log(
            "Farm boundary:",
            layer.getLatLngs()
        );

        console.log(
            "Farm area:",
            areaHectares,
            "hectares"
        );

    }
);

}

    }

    catch (error) {

        console.error(
            "AgriVision NDVI Error:",
            error
        );

        status.innerText =
            "❌ Could not connect to AgriVision NDVI server.";

    }

}
/* ============================================================
   AGRIVISION - FINAL WEATHER + CALENDAR + CROP DOCTOR
   ============================================================ */

const AGRIVISION_API = "http://127.0.0.1:5000";


/* ============================================================
   1. WEATHER
   ============================================================ */

async function loadWeather() {

    const result =
        document.getElementById("weatherResult");

    const lat =
        parseFloat(
            document.getElementById("latitude").value
        );

    const lon =
        parseFloat(
            document.getElementById("longitude").value
        );

    if (
        Number.isNaN(lat) ||
        Number.isNaN(lon)
    ) {
        result.innerHTML =
            "❌ Please enter a valid farm location.";

        return;
    }

    result.innerHTML =
        "☁️ Loading live weather...";

    try {

        const response =
            await fetch(
                AGRIVISION_API +
                "/api/weather" +
                "?lat=" +
                encodeURIComponent(lat) +
                "&lon=" +
                encodeURIComponent(lon)
            );

        const data =
            await response.json();

        if (!response.ok || !data.success) {

            throw new Error(
                data.error ||
                "Weather service unavailable."
            );
        }

        const current =
            data.current || {};

        const hourly =
            data.hourly || {};

        const daily =
            data.daily || {};

        const temperature =
            current.temperature_2m ?? "--";

        const humidity =
            current.relative_humidity_2m ?? "--";

        const rain =
            current.rain ?? current.precipitation ?? 0;

        const wind =
            current.wind_speed_10m ?? "--";

        const rainChance =
            hourly
            .precipitation_probability?.[0] ?? 0;

        const soilMoisture =
            hourly
            .soil_moisture_0_to_1cm?.[0] ?? "--";

        const maxTemp =
            daily.temperature_2m_max?.[0] ?? "--";

        const minTemp =
            daily.temperature_2m_min?.[0] ?? "--";

        const rainfall7 =
            daily.precipitation_sum
            ?.reduce(
                (a, b) => a + (Number(b) || 0),
                0
            )
            .toFixed(1) ?? "--";


        result.innerHTML = `

            <div class="weather-grid">

                <div class="weather-item">
                    <div class="feature-icon">🌡️</div>
                    <strong>${temperature} °C</strong>
                    <span>Temperature</span>
                </div>

                <div class="weather-item">
                    <div class="feature-icon">💧</div>
                    <strong>${humidity}%</strong>
                    <span>Humidity</span>
                </div>

                <div class="weather-item">
                    <div class="feature-icon">🌧️</div>
                    <strong>${rain} mm</strong>
                    <span>Current Rain</span>
                </div>

                <div class="weather-item">
                    <div class="feature-icon">☔</div>
                    <strong>${rainChance}%</strong>
                    <span>Rain Probability</span>
                </div>

                <div class="weather-item">
                    <div class="feature-icon">🌱</div>
                    <strong>${soilMoisture}</strong>
                    <span>Soil Moisture</span>
                </div>

                <div class="weather-item">
                    <div class="feature-icon">💨</div>
                    <strong>${wind} km/h</strong>
                    <span>Wind Speed</span>
                </div>

            </div>

            <hr>

            <h3>🌤️ Today's Forecast</h3>

            <p>
                Minimum Temperature:
                <strong>${minTemp} °C</strong>
            </p>

            <p>
                Maximum Temperature:
                <strong>${maxTemp} °C</strong>
            </p>

            <p>
                🌧️ Expected rainfall over
                the next 7 days:
                <strong>${rainfall7} mm</strong>
            </p>

            <hr>

            <h3>🌾 Farming Recommendation</h3>

            <p>
                ${
                    Number(rainChance) >= 60
                    ?
                    "🌧️ Rain is likely. Avoid unnecessary irrigation and check field drainage."
                    :
                    Number(temperature) >= 35
                    ?
                    "🔥 High temperature detected. Monitor crop water stress and irrigation."
                    :
                    "✅ Weather conditions appear suitable. Continue regular crop monitoring."
                }
            </p>
        `;

    }

    catch (error) {

        console.error(
            "Weather error:",
            error
        );

        result.innerHTML =
            "❌ Weather error: " +
            error.message;
    }
}



/* ============================================================
   2. CROP CALENDAR
   ============================================================ */

async function loadCalendar() {
    const crop =
        document.getElementById(
            "calendarCrop"
        ).value;

    const result =
        document.getElementById(
            "calendarResult"
        );

    result.innerHTML =
        "Loading crop calendar...";

    try {
        const response =
            await fetch(
                API +
                "/api/calendar?crop=" +
                encodeURIComponent(crop)
            );

        const data =
            await response.json();

        if (!response.ok || !data.success) {
            throw new Error(
                data.error ||
                "Could not load crop calendar."
            );
        }

        const stages =
            data.calendar
                .map(
                    item =>
                        `<li>
                            <strong>${escapeHTML(item.stage)}</strong>
                            — ${escapeHTML(item.period)}
                            <br>
                            ${escapeHTML(item.activity)}
                        </li>`
                )
                .join("");

        result.innerHTML = `
            <h3>${escapeHTML(data.crop)}</h3>

            <p>
                <strong>Season:</strong>
                ${escapeHTML(data.season)}
            </p>

            <p>
                <strong>Typical duration:</strong>
                ${escapeHTML(data.duration)}
            </p>

            <ol>
                ${stages}
            </ol>
        `;
    } catch (error) {
        result.innerText =
            "❌ Calendar error: " +
            error.message;
    }
}

/* ============================================================
   3. CROP DOCTOR
   ============================================================ */

function setupCropDoctor() {

    const module =
        document.getElementById(
            "doctorModule"
        );

    if (!module) {
        return;
    }

    const uploadBox =
        module.querySelector(
            ".upload-box"
        );

    if (!uploadBox) {
        return;
    }


    /* Crop selector */

    if (
        !document.getElementById(
            "doctorCrop"
        )
    ) {

        const cropDiv =
            document.createElement(
                "div"
            );

        cropDiv.style.margin =
            "15px 0";

        cropDiv.innerHTML = `

            <label
                for="doctorCrop"
                style="
                    display:block;
                    font-weight:bold;
                    margin-bottom:8px;
                "
            >
                🌱 Select Crop
            </label>

            <select
                id="doctorCrop"
                style="
                    width:100%;
                    padding:12px;
                    border-radius:8px;
                    border:1px solid #ccc;
                    font-size:16px;
                "
            >

                <option value="rice">
                    🌾 Rice
                </option>

                <option value="jasmine">
                    🌼 Jasmine
                </option>

                <option value="groundnut">
                    🥜 Groundnut
                </option>

                <option value="chilli">
                    🌶️ Chilli
                </option>

            </select>

        `;

        uploadBox.insertBefore(
            cropDiv,
            uploadBox.firstChild
        );
    }


    /* Analyze button */

    if (
        !document.getElementById(
            "doctorAnalyzeBtn"
        )
    ) {

        const button =
            document.createElement(
                "button"
            );

        button.id =
            "doctorAnalyzeBtn";

        button.className =
            "btn";

        button.style.marginTop =
            "15px";

        button.innerHTML =
            "🩺 Analyze Crop";

        button.onclick =
            analyzeCrop;

        uploadBox.appendChild(
            button
        );
    }

}



/* ============================================================
   CROP DOCTOR ANALYSIS
   ============================================================ */

async function analyzeCrop() {

    const input =
        document.getElementById(
            "cropImageInput"
        );

    const crop =
        document.getElementById(
            "doctorCrop"
        ).value;

    const status =
        document.getElementById(
            "doctorStatus"
        );

    if (
        !input ||
        !input.files ||
        !input.files[0]
    ) {

        status.innerHTML =
            "⚠️ Please upload a crop image first.";

        return;
    }


    const file =
        input.files[0];


    status.innerHTML =
        "🔬 Sending image to AgriVision Crop Doctor...";


    try {

        const formData =
            new FormData();

        formData.append(
            "crop",
            crop
        );

        formData.append(
            "image",
            file
        );


        const response =
            await fetch(
                AGRIVISION_API +
                "/api/crop-doctor",
                {
                    method: "POST",
                    body: formData
                }
            );


        const data =
            await response.json();


        if (
            !response.ok ||
            !data.success
        ) {

            throw new Error(
                data.error ||
                "Crop Doctor failed."
            );
        }


        status.innerHTML = `

            <div
                style="
                    margin-top:15px;
                    padding:18px;
                    background:#f1f8e9;
                    border-radius:12px;
                    border-left:5px solid #2e7d32;
                "
            >

                <h3>
                    🩺 Crop Doctor Result
                </h3>

                <p>
                    <strong>Crop:</strong>
                    ${data.crop}
                </p>

                <p>
                    <strong>Status:</strong>
                    ${data.status}
                </p>

                <p>
                    <strong>Analysis:</strong>
                    ${data.diagnosis}
                </p>

                <p>
                    🌱 ${data.general_advice}
                </p>

                <hr>

                <p>
                    ℹ️ ${data.message}
                </p>

                <small>
                    Model status:
                    ${data.model_status}
                </small>

            </div>

        `;

    }

    catch (error) {

        console.error(
            "Crop Doctor error:",
            error
        );

        status.innerHTML =
            "❌ Crop Doctor error: " +
            error.message;
    }

}



/* ============================================================
   UPDATE CALENDAR CROPS
   ============================================================ */

function updateCalendarCropOptions() {

    const select =
        document.getElementById(
            "calendarCrop"
        );

    if (!select) {
        return;
    }

    select.innerHTML = `

        <option value="rice">
            🌾 Rice
        </option>

        <option value="jasmine">
            🌼 Jasmine
        </option>

        <option value="groundnut">
            🥜 Groundnut
        </option>

        <option value="chilli">
            🌶️ Chilli
        </option>

    `;

}



/* ============================================================
   START AGRIVISION MODULES
   ============================================================ */

document.addEventListener(
    "DOMContentLoaded",
    function () {

        updateCalendarCropOptions();

        setupCropDoctor();

        console.log(
            "✅ AgriVision Weather + Calendar + Crop Doctor ready"
        );

    }
);